"""NPC 基于自身需要、既有目标与真实地点推进日常生活。"""

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso
from world_engine.schedules import ScheduleService
from world_engine.routines import RoutinePlanSpec, RoutineService


class DailyLifeService:
    @classmethod
    def _rest_at_home(cls, c, actor, at, minutes):
        from world_engine.interiors import InteriorService

        if actor["current_room_id"] and c.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging' AND status='active' AND asset_id=? AND requester_id!=? AND due_world_time>?",(actor["current_room_id"],actor["id"],to_iso(at))).fetchone():
            location=c.execute("SELECT * FROM locations WHERE id=?",(actor["current_location_id"] or actor["location_id"],)).fetchone()
            return cls._travel(c,actor,at,location)
        # 只沿真实入口进入自己的空房或有效租住房，不占用已出租给别人的房间。
        rooms = c.execute(
            "SELECT r.* FROM life_rooms r WHERE r.world_id=? AND r.location_id=? AND "
            "((r.owner_character_id=? AND NOT EXISTS (SELECT 1 FROM contract_fulfillments f "
            "WHERE f.asset_id=r.id AND f.kind='lodging' AND f.status='active')) OR EXISTS "
            "(SELECT 1 FROM contract_fulfillments f WHERE f.asset_id=r.id AND f.kind='lodging' "
            "AND f.status='active' AND f.requester_id=? AND f.due_world_time>?)) ORDER BY r.id",
            (
                actor["world_id"],
                actor["current_location_id"] or actor["location_id"],
                actor["id"],
                actor["id"],
                to_iso(at),
            ),
        ).fetchall()
        for room in rooms:
            if actor["current_room_id"] == room["id"]:
                break
            path = [room]
            while path[-1]["parent_room_id"]:
                path.append(InteriorService.room(c, actor["world_id"], path[-1]["parent_room_id"]))
            entrance = next(
                (
                    part
                    for part in reversed(path)
                    if part["parent_room_id"] == actor["current_room_id"]
                ),
                None,
            )
            if entrance is None or not InteriorService.allowed(c, entrance, actor["id"]):
                continue
            if entrance["door_locked"] and not InteriorService.keyed(c, entrance, actor["id"]):
                continue
            if entrance["door_locked"]:
                InteriorService.door(c, actor, entrance["id"], "unlock", at)
            InteriorService.door(c, actor, entrance["id"], "open", at)
            InteriorService.door(c, actor, entrance["id"], "enter", at)
            if entrance["id"] != room["id"]:
                return "经过住所入口"
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actor["id"],)).fetchone()
            break
        return cls._start(c, actor, at, "rest", minutes)

    @classmethod
    def _find_work(cls, c, actor, at, locations, plan=None):
        saved=c.execute("SELECT spec_json FROM npc_routine_plans WHERE character_id=?",(actor["id"],)).fetchone()
        if saved:
            routine_spec=RoutinePlanSpec.model_validate_json(saved[0])
            if routine_spec.enabled and RoutineService.work_remaining(c,actor,routine_spec,at)<60:
                return "今日自动劳动安排已达到限额，先处理其他生活需要"
        remaining = ScheduleService.planned_minutes(plan, at)
        if remaining is not None and remaining < 60:
            return "留出时间准备赴约"
        accounts = {
            row[0]
            for row in c.execute(
                "SELECT location_id FROM workplace_accounts WHERE world_id=? AND balance>=wage",
                (actor["world_id"],),
            )
        }
        workplaces = [
            loc
            for loc in locations
            if loc["id"] in accounts
            or loc["kind"] == "workplace"
            and not c.execute(
                "SELECT 1 FROM workplace_accounts WHERE location_id=?", (loc["id"],)
            ).fetchone()
        ]
        if not workplaces:
            return "附近暂时没有可做的工作"
        destination = min(
            workplaces,
            key=lambda loc: great_circle_distance_km(
                actor["longitude"], actor["latitude"], loc["longitude"], loc["latitude"]
            ),
        )
        if (
            actor["current_room_id"]
            or great_circle_distance_km(
                actor["longitude"],
                actor["latitude"],
                destination["longitude"],
                destination["latitude"],
            )
            > 0.1
        ):
            return cls._travel(c, actor, at, destination)
        return cls._start(c, actor, at, "work")

    @staticmethod
    def _start(c, actor, at, kind, minutes=60, recipe_id=None):
        from world_engine.actions import ActionService
        from world_engine.activity_tasks import TaskService

        if kind == "rest":
            aid = LifeActivityService.start(c, actor, at, "rest", minutes)
            name = "休息"
        else:
            aid, name, minutes = TaskService.start(c, actor, at, recipe_id)
        event = ActionService._record_event(
            c,
            world_id=actor["world_id"],
            tick_id=str(uuid4()),
            occurred_at=at,
            event_type="action.npc_activity_started",
            actor_id=actor["id"],
            target_id=None,
            location_id=actor["current_location_id"] or actor["location_id"],
            summary=f"{actor['name']}开始{name}，计划持续{minutes}分钟。",
            payload={"activity_id": aid},
        )
        c.execute("UPDATE character_life_activities SET source_event_id=? WHERE id=?", (event, aid))
        TaskService.attach_source(c, aid, actor["world_id"], actor["id"], event)
        return name

    @staticmethod
    def _travel(c, actor, at, location):
        from world_engine.interiors import InteriorService
        from world_engine.movement import MovementService

        if actor["current_room_id"]:
            room = InteriorService.room(c, actor["world_id"], actor["current_room_id"])
            if actor["current_fixture_id"]:
                InteriorService.operate_fixture(c, actor, actor["current_fixture_id"], "stand", at)
                actor = c.execute("SELECT * FROM characters WHERE id=?", (actor["id"],)).fetchone()
            if room["door_locked"]:
                InteriorService.door(c, actor, room["id"], "unlock", at)
            InteriorService.door(c, actor, room["id"], "open", at)
            InteriorService.door(c, actor, room["id"], "exit", at)
            return "离开房间"
        MovementService().start(
            c,
            world_id=actor["world_id"],
            character_id=actor["id"],
            destination_longitude=location["longitude"],
            destination_latitude=location["latitude"],
            world_time=at,
        )
        return f"前往{location['name']}"

    @classmethod
    def tick(cls, c, wid, at, *, adjudication_due=False):
        from world_engine.actions import ActionService
        from world_engine.activity_tasks import TaskService
        from world_engine.domain import ActionProposal, ActionType
        from world_engine.economy import EconomyService

        slot = at.strftime("%Y-%m-%dT%H")
        RoutineService.finish_due(c,wid,at)
        npcs = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND health>0 ORDER BY id",
            (wid,),
        ).fetchall()
        locations = c.execute(
            "SELECT * FROM locations WHERE world_id=? AND is_active=1", (wid,)
        ).fetchall()
        by_id = {row["id"]: row for row in locations}
        for npc in npcs:
            c.execute(
                "INSERT OR IGNORE INTO npc_daily_states(character_id,world_id) VALUES (?,?)",
                (npc["id"], wid),
            )
            state = c.execute(
                "SELECT * FROM npc_daily_states WHERE character_id=?", (npc["id"],)
            ).fetchone()
            plan = ScheduleService.assess(c, npc, at)
            changed = ScheduleService.record_assessment(c, npc, at, plan)
            routine=RoutineService.context(c,npc,at)
            from world_engine.npc_goals import NpcGoalService
            has_goals=bool(routine and routine["spec"].ambitions)
            if has_goals:
                NpcGoalService.refresh(c,npc,at)
            changed=changed or bool(routine and routine["changed"])
            next_check = from_iso(state["next_world_time"]) if state["next_world_time"] else at
            critical = npc["health"] < 40 or npc["satiety"] <= 10 or npc["energy"] <= 10
            if (adjudication_due and (routine is None or routine["slot"] is None) and not has_goals) or (
                state["last_slot"] == slot and not changed and at < next_check and not critical
            ):
                continue
            active = LifeActivityService.running(c, npc["id"])
            if (
                active
                and active["kind"] in {"work", "craft", "repair"}
                and (npc["health"] < 40 or npc["satiety"] <= 10 or npc["energy"] <= 10)
            ):
                LifeActivityService.finish(
                    c, active, at, "interrupted", "身体状况需要先处理，暂缓当前安排"
                )
                npc = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
                active = None
            if (
                active
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (npc["id"],),
                ).fetchone()
            ):
                if routine and routine.get("occurrence"):
                    linked=active and c.execute("SELECT 1 FROM npc_routine_activities WHERE occurrence_id=? AND activity_id=?",(routine["occurrence"]["id"],active["id"])).fetchone()
                    if not linked:
                        RoutineService.defer(c,routine,at,"正在处理已有活动或行程，作息暂缓")
                continue
            intention = "处理自己的事务"
            c.execute("SAVEPOINT npc_daily_action")
            try:
                EconomyService.collect_outputs(c, npc, at)
                npc = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
                local_places = [
                    loc
                    for loc in locations
                    if loc["id"] == npc["location_id"]
                    or great_circle_distance_km(
                        npc["longitude"], npc["latitude"], loc["longitude"], loc["latitude"]
                    )
                    <= 10
                ]
                traits = json.loads(npc["traits_json"] or "[]")
                night_worker = "巡夜" in (npc["identity"] or "") or "夜行" in traits
                sleep_time = (7 <= at.hour < 15) if night_worker else (at.hour >= 22 or at.hour < 6)

                def near(loc, actor=npc):
                    return (
                        great_circle_distance_km(
                            actor["longitude"], actor["latitude"], loc["longitude"], loc["latitude"]
                        )
                        <= 0.1
                    )

                remaining = ScheduleService.planned_minutes(plan, at)
                if routine and routine["next_boundary"]>at:
                    until_boundary=max(0,int((routine["next_boundary"]-at).total_seconds()//60))
                    remaining=until_boundary if remaining is None else min(remaining,until_boundary)
                urgent_care = npc["health"] < 40 or npc["satiety"] <= 10 or npc["energy"] <= 10
                if plan and plan["due"] and not urgent_care and plan["contract"]["is_active"]:
                    RoutineService.defer(c,routine,at,"优先处理已经确认的见面约定")
                    destination = by_id.get(plan["contract"]["location_id"])
                    intention = (
                        cls._travel(c, npc, at, destination)
                        if npc["current_room_id"] or not near(destination)
                        else "已到约定地点，等候对方"
                    )
                elif npc["satiety"] <= 30:
                    RoutineService.defer(c,routine,at,"先解决用饭需要，再继续日常安排")
                    meal = ActionService().execute(
                        c,
                        world_id=wid,
                        tick_id=str(uuid4()),
                        occurred_at=at,
                        proposal=ActionProposal(
                            actor_id=npc["id"], action=ActionType.EAT, reason="饱食不足，先用饭"
                        ),
                    )
                    if meal.accepted:
                        intention = "用饭"
                    else:
                        if EconomyService.buy_food(c, npc, at):
                            intention = "买到了真实食物"
                        elif EconomyService.harvest(c, npc, at, food_only=True):
                            intention = "收取可以食用的资源"
                        elif npc["money"] < 3:
                            intention = (
                                cls._rest_at_home(c, npc, at, 120)
                                if npc["energy"] < 20
                                else cls._find_work(c, npc, at, local_places, plan)
                            )
                        food_places = [
                            loc
                            for loc in local_places
                            if json.loads(loc["resources_json"] or "{}").get("food", 0) > 0
                        ]
                        food_ids = {
                            row[0]
                            for row in c.execute(
                                "SELECT resource_location_id FROM world_item_profiles WHERE world_id=? AND nutrition>0 AND (resource_owner_id IS NULL OR resource_owner_id=?)",
                                (wid, npc["id"]),
                            )
                        }
                        food_places.extend(
                            loc
                            for loc in local_places
                            if loc["id"] in food_ids and loc not in food_places
                        )
                        if food_places and intention == "处理自己的事务":
                            destination = min(
                                food_places,
                                key=lambda loc: great_circle_distance_km(
                                    npc["longitude"],
                                    npc["latitude"],
                                    loc["longitude"],
                                    loc["latitude"],
                                ),
                            )
                            if npc["current_room_id"] or not near(destination):
                                intention = cls._travel(c, npc, at, destination)
                            else:
                                intention = (
                                    cls._rest_at_home(c, npc, at, 120)
                                    if npc["energy"] < 20
                                    else cls._find_work(c, npc, at, local_places, plan)
                                )
                elif npc["energy"] <= 25 or npc["health"] < 40 or (sleep_time and (routine is None or routine["slot"] is None)):
                    RoutineService.defer(c,routine,at,"身体需要休息，暂缓原定安排")
                    home = by_id.get(npc["location_id"])
                    if home and not near(home):
                        intention = cls._travel(c, npc, at, home)
                    else:
                        minutes = 120 if sleep_time else 60
                        if remaining is not None and not urgent_care:
                            minutes = min(minutes, max(1, remaining))
                        intention = cls._rest_at_home(c, npc, at, minutes)
                elif routine is not None:
                    if has_goals and routine["slot"] is None:
                        intention=NpcGoalService.perform(c,npc,at,routine,plan) or "按自己的作息自由安排"
                    else:
                        intention=RoutineService.perform(c,npc,at,routine,plan)
                else:
                    production = TaskService.available(c, npc)
                    if production and any(
                        word in (npc["identity"] or "") for word in ("商", "匠", "厨", "店", "医")
                    ):
                        for recipe in production:
                            if remaining is not None and recipe["duration_minutes"] > remaining:
                                continue
                            if recipe["kind"] == "craft":
                                try:
                                    intention = cls._start(
                                        c, npc, at, "craft", recipe_id=recipe["id"]
                                    )
                                    break
                                except ValueError:
                                    continue
                    if intention == "处理自己的事务":
                        harvested = EconomyService.harvest(c, npc, at)
                        if harvested:
                            intention = "收取原料"
                        else:
                            intention = cls._find_work(c, npc, at, local_places, plan)
                c.execute("RELEASE SAVEPOINT npc_daily_action")
                next_time = at + timedelta(hours=1)
                if has_goals:
                    goal_retry=c.execute("SELECT MIN(retry_world_time) FROM npc_life_goals WHERE character_id=? AND status IN ('active','blocked') AND retry_world_time>?",(npc["id"],to_iso(at))).fetchone()[0]
                    if goal_retry:
                        next_time=min(next_time,from_iso(goal_retry))
                if routine:
                    if routine["next_boundary"]>at:
                        next_time=min(next_time,routine["next_boundary"])
                    if routine.get("occurrence"):
                        retry=c.execute("SELECT next_attempt_world_time FROM npc_routine_occurrences WHERE id=?",(routine["occurrence"]["id"],)).fetchone()
                        if retry and retry[0] and from_iso(retry[0])>at:
                            next_time=min(next_time,from_iso(retry[0]))
                if plan and plan["depart_at"] > at:
                    next_time = min(next_time, plan["depart_at"])
                if intention in {"离开房间", "经过住所入口"}:
                    next_time = min(next_time, at + timedelta(minutes=5))
                active = LifeActivityService.running(c, npc["id"])
                if active:
                    next_time = min(next_time, from_iso(active["ends_world_time"]))
                movement = c.execute(
                    "SELECT estimated_arrival_world FROM character_movements WHERE character_id=? AND status='moving'",
                    (npc["id"],),
                ).fetchone()
                if movement:
                    next_time = min(next_time, from_iso(movement[0]))
                c.execute(
                    "UPDATE npc_daily_states SET last_slot=?,intention=?,next_world_time=?,last_error=NULL WHERE character_id=?",
                    (slot, intention, to_iso(next_time), npc["id"]),
                )
            except ValueError as exc:
                c.execute("ROLLBACK TO SAVEPOINT npc_daily_action")
                c.execute("RELEASE SAVEPOINT npc_daily_action")
                c.execute(
                    "UPDATE npc_daily_states SET last_slot=?,intention=?,last_error=? WHERE character_id=?",
                    (slot, "当前条件不足，暂缓计划", str(exc)[:300], npc["id"]),
                )
