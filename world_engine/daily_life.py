"""NPC 基于自身需要、既有目标与真实地点推进日常生活。"""

import json
import logging
from datetime import timedelta
from uuid import uuid4

from world_engine.diagnostic_logging import diagnostic_json
from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso
from world_engine.routines import RoutinePlanSpec, RoutineService
from world_engine.schedules import ScheduleService

LOGGER = logging.getLogger("virtual-world.worker")


class DailyLifeService:
    @staticmethod
    def is_sleep_time(actor, at):
        """哥哥，日常生活和科研共用昼夜安排，巡夜居民白天也需要睡觉。"""
        traits = json.loads(actor["traits_json"] or "[]")
        night_worker = "巡夜" in (actor["identity"] or "") or "夜行" in traits
        return (7 <= at.hour < 15) if night_worker else (at.hour >= 22 or at.hour < 6)

    @classmethod
    def _rest_at_home(cls, c, actor, at, minutes):
        from world_engine.interiors import InteriorService
        from world_engine.families import FamilyService

        shared_home=FamilyService.home(c,actor["world_id"],actor["id"],at)
        if shared_home and not InteriorService.near_location(c,actor,shared_home["location_id"]):
            location=c.execute("SELECT * FROM locations WHERE id=?",(shared_home["location_id"],)).fetchone()
            return cls._travel(c,actor,at,location)

        if actor["current_room_id"] and not (shared_home and shared_home["id"]==actor["current_room_id"]) and c.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging' AND status='active' AND asset_id=? AND requester_id!=? AND due_world_time>?",(actor["current_room_id"],actor["id"],to_iso(at))).fetchone():
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
        if shared_home:
            rooms=[shared_home]+[room for room in rooms if room["id"]!=shared_home["id"]]
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
        from world_engine.overnight import OvernightService
        night_remaining = OvernightService.planned_minutes(c, actor, at)
        if night_remaining is not None:
            remaining = night_remaining if remaining is None else min(remaining, night_remaining)
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
        # 哥哥，新工作还需保留从工作地点实际赶回住宿的时间，不能先离开再发现来不及。
        shadow = {
            **dict(actor), "longitude": destination["longitude"],
            "latitude": destination["latitude"], "current_room_id": None,
        }
        night_remaining = OvernightService.planned_minutes(c, shadow, at)
        if night_remaining is not None and (
            ScheduleService.earliest_arrival(c, actor, destination, at) + timedelta(minutes=60)
            > at + timedelta(minutes=night_remaining)
        ):
            return "留出时间准备已确认的长时住宿"
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
        from world_engine.domain import ActionProposal, ActionType

        before = ActionService._observed_state(c, actor["id"])
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
        ActionService._record_effect(
            c, actor["world_id"], event,
            ActionProposal(actor_id=actor["id"], action=ActionType.REST if kind == "rest" else ActionType.ACTIVITY,
                           reason="依据生活需求和已采纳的安排开始活动"),
            at, before, ActionService._observed_state(c, actor["id"]), None, None,
            "action.npc_activity_started",
        )
        return name

    @staticmethod
    def food_places(
        c, world_id: str, candidate_ids: list[str], *, actor_id: str
    ) -> list[str]:
        """从候选地点里筛出当前可获得食物的地点，返回地点 id。

        判定规则与原实现一致，额外支持通用资源：
        - 地点绑定了 food profile（`resource_location_id` 精确匹配，或属于该角色）
        - 或存在通用 food profile（`resource_location_id IS NULL`，任何地点可采）
        两种情况都还要求该地点当前 `resources_json.food > 0`。
        """
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        bound_rows = c.execute(
            f"SELECT resource_location_id FROM world_item_profiles "
            f"WHERE world_id=? AND resource_key='food' AND nutrition>0 "
            f"AND (resource_owner_id IS NULL OR resource_owner_id=?) "
            f"AND resource_location_id IS NOT NULL "
            f"AND resource_location_id IN ({placeholders})",  # noqa: S608 - 占位符按候选数生成
            (world_id, actor_id, *candidate_ids),
        ).fetchall()
        has_generic = bool(
            c.execute(
                "SELECT 1 FROM world_item_profiles WHERE world_id=? AND resource_key='food' "
                "AND nutrition>0 AND (resource_owner_id IS NULL OR resource_owner_id=?) "
                "AND resource_location_id IS NULL",
                (world_id, actor_id),
            ).fetchone()
        )
        bound = {row["resource_location_id"] for row in bound_rows}
        result: list[str] = []
        for place_id in candidate_ids:
            if place_id not in bound and not has_generic:
                continue
            row = c.execute(
                "SELECT resources_json FROM locations WHERE id=?", (place_id,)
            ).fetchone()
            if row is None:
                continue
            if int(json.loads(row["resources_json"] or "{}").get("food", 0)) > 0:
                result.append(place_id)
        return result

    @staticmethod
    def known_food_places(c, actor, at, candidate_ids):
        """哥哥，只凭本人亲历或目击过的采集选择地点，不读取远处当前库存。"""
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        # 哥哥，单地点最多返回一个结果；从本人知识出发避免扫描全世界的事件，
        # 多地点保留原查询顺序，以免等距时改变既有目的地选择。
        source = (
            "SELECT DISTINCT e.location_id FROM character_event_knowledge k "
            "CROSS JOIN world_events e ON k.event_id=e.id AND k.world_id=e.world_id "
            if len(candidate_ids) == 1 else
            "SELECT DISTINCT e.location_id FROM world_events e "
            "JOIN character_event_knowledge k ON k.event_id=e.id AND k.world_id=e.world_id "
        )
        rows = c.execute(
            source +
            "JOIN event_observers o ON o.event_id=e.id AND o.character_id=k.character_id "
            "JOIN world_item_profiles p ON p.world_id=e.world_id AND p.item_type_id="
            "CASE WHEN json_valid(e.payload_json) THEN json_extract(e.payload_json,'$.item_type_id') END "
            "JOIN locations l ON l.id=e.location_id AND l.world_id=e.world_id "
            "WHERE e.world_id=? AND k.character_id=? AND e.event_type='action.resource_harvested' "
            "AND k.source_kind='experienced' AND o.channel IN ('participant','witnessed') "
            "AND e.occurred_at<=? AND k.learned_world_time<=? "
            "AND p.nutrition>0 AND (p.resource_owner_id IS NULL OR p.resource_owner_id=?) "
            "AND (p.resource_location_id IS NULL OR p.resource_location_id=e.location_id) "
            "AND l.is_active=1 AND l.space_layer=? AND e.location_id IN (" + placeholders + ")",
            (actor["world_id"], actor["id"], to_iso(at), to_iso(at), actor["id"], actor["space_layer"], *candidate_ids),
        ).fetchall()
        return [row["location_id"] for row in rows]

    @staticmethod
    def familiar_place_to_search(c, actor, at, locations):
        """哥哥，找不到已知食物时，可到本人熟悉的当前地点实地查看。"""
        place = locations.get(actor["current_location_id"])
        if (not place or place["world_id"] != actor["world_id"] or not place["is_active"]
                or place["space_layer"] != actor["space_layer"]):
            return None
        distance = great_circle_distance_km(actor["longitude"], actor["latitude"], place["longitude"], place["latitude"])
        if not 0.1 < distance <= min(1.0, float(place["area_radius_km"] or 0)):
            return None
        # 哥哥，人物实际所属的公共城镇是本人已知位置，但不代表知道远处库存。
        if place["kind"] in {"city", "town"}:
            return place
        movements = c.execute(
            "SELECT m.origin_longitude,m.origin_latitude FROM character_movements m "
            "JOIN world_events e ON e.world_id=m.world_id AND e.actor_id=m.character_id "
            "AND e.event_type='action.movement_started' AND CASE WHEN json_valid(e.payload_json) "
            "THEN json_extract(e.payload_json,'$.movement_id')=m.id ELSE 0 END "
            "JOIN character_event_knowledge k ON k.world_id=e.world_id AND k.event_id=e.id AND k.character_id=m.character_id "
            "JOIN event_observers o ON o.event_id=e.id AND o.character_id=k.character_id "
            "WHERE m.world_id=? AND m.character_id=? AND e.location_id=? AND e.occurred_at=m.started_at_world "
            "AND e.occurred_at<=? AND k.learned_world_time<=? "
            "AND k.source_kind='experienced' AND o.channel='participant'",
            (actor["world_id"], actor["id"], place["id"], to_iso(at), to_iso(at)),
        ).fetchall()
        if any(great_circle_distance_km(row["origin_longitude"], row["origin_latitude"],
                                        place["longitude"], place["latitude"]) <= 0.1
               for row in movements):
            # 哥哥，仅知道去过这里；不查询远处食物当前是否存在。
            return place
        return None

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

    @staticmethod
    def _visible_appointment_partner(c, npc, plan, at, place):
        """哥哥，只靠已确认约定和本人眼前的人决定是否走近。"""
        from world_engine.appointments import AppointmentService
        from world_engine.intimacy import IntimacyService
        from world_engine.player_steering import PlayerSteeringService
        from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
        from world_engine.rides import RideService

        date = plan.get("appointment") if plan else None
        if (not date or date["world_id"] != npc["world_id"] or date["status"] != "accepted"
                or date["room_id"] is not None or not plan["due"] or not place or not place["is_active"]
                or date["contract_id"] != plan["contract"]["request_id"]
                or not from_iso(date["starts_at"]) <= at
                or at + timedelta(minutes=date["minutes"]) > from_iso(date["ends_at"])
                or npc["id"] not in {date["proposer_id"], date["recipient_id"]}
                or npc["health"] < 40 or npc["satiety"] <= 10 or npc["energy"] <= 10
                or npc["current_fixture_id"] or npc["active_vehicle_id"]
                or PlayerSteeringService.active(c, npc["id"])
                or not AppointmentService._at_place(c, npc, place)):
            return None
        other_id = date["recipient_id"] if date["proposer_id"] == npc["id"] else date["proposer_id"]
        player = c.execute("SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=1 AND is_pov=1",
                           (other_id, npc["world_id"])).fetchone()
        if (not player or player["current_fixture_id"] or player["active_vehicle_id"]
                or not AppointmentService._at_place(c, player, place)
                or not same_room(npc, player) or RideService.aboard(c, player["id"])
                or PlayerSteeringService.active(c, player["id"])
                or c.execute("SELECT 1 FROM character_movements WHERE world_id=? AND character_id=? "
                             "AND status='moving'", (npc["world_id"], player["id"])).fetchone()
                or IntimacyService.paused(c, npc["world_id"], npc["id"], player["id"])):
            return None
        # 哥哥，走近是本次约定的一小步，不能借独立移动暂停任何人的旧同行授权。
        for person_id in (npc["id"], player["id"]):
            if (c.execute("SELECT 1 FROM companion_sessions WHERE world_id=? AND status='active' "
                          "AND (leader_id=? OR companion_id=?)", (npc["world_id"], person_id, person_id)).fetchone()
                    or c.execute("SELECT 1 FROM joint_journeys WHERE world_id=? "
                                 "AND status IN ('accepted','active','paused','resume_pending') "
                                 "AND (proposer_id=? OR recipient_id=?)", (npc["world_id"], person_id, person_id)).fetchone()
                    or c.execute("SELECT 1 FROM group_journeys g JOIN group_journey_members m ON m.journey_id=g.id "
                                 "WHERE g.world_id=? AND m.character_id=? AND m.left_at IS NULL "
                                 "AND g.status IN ('accepted','active','paused','resume_pending')",
                                 (npc["world_id"], person_id)).fetchone()):
                return None
        active = LifeActivityService.running(c, player["id"])
        if active and active["kind"] != "wait":
            return None
        distance = great_circle_distance_km(npc["longitude"], npc["latitude"], player["longitude"], player["latitude"])
        if not 0.02 < distance <= VISIBLE_PERSON_RADIUS_KM:
            return None
        # 哥哥，确认事件及双方亲历知识必须仍然存在，不拿记录状态代替同意。
        event = c.execute(
            "SELECT e.id,e.occurred_at FROM world_events e "
            "WHERE e.id=? AND e.world_id=? AND e.event_type='social.appointment_accepted' "
            "AND ((e.actor_id=? AND e.target_id=?) OR (e.actor_id=? AND e.target_id=?)) "
            "AND e.occurred_at<=? AND CASE WHEN json_valid(e.payload_json) "
            "THEN json_extract(e.payload_json,'$.appointment_id')=? ELSE 0 END",
            (date["source_event_id"], npc["world_id"], npc["id"], player["id"], player["id"], npc["id"], to_iso(at), date["id"]),
        ).fetchone()
        if not event:
            return None
        known = c.execute(
            "SELECT COUNT(DISTINCT k.character_id) FROM character_event_knowledge k "
            "JOIN event_observers o ON o.event_id=k.event_id AND o.character_id=k.character_id "
            "WHERE k.event_id=? AND k.world_id=? AND k.character_id IN (?,?) "
            "AND k.source_kind='experienced' AND o.channel='participant' AND k.learned_world_time>=? AND k.learned_world_time<=?",
            (event["id"], npc["world_id"], npc["id"], player["id"], event["occurred_at"], to_iso(at)),
        ).fetchone()[0]
        return player if known == 2 else None

    @staticmethod
    def _approach_appointment_partner(c, npc, player, date, at):
        """本人走向眼前位置，最终是否开始相处仍由玩家单独选择。"""
        from world_engine.movement import MovementService

        movement = MovementService().start(
            c, world_id=npc["world_id"], character_id=npc["id"],
            destination_longitude=player["longitude"], destination_latitude=player["latitude"],
            world_time=at, minimum_distance_km=0.001,
        )
        if movement.estimated_arrival_world + timedelta(minutes=date["minutes"]) > from_iso(date["ends_at"]):
            raise ValueError("剩余时间不足以完成真实靠近和约定相处")
        return "按本人已确认的约定走近眼前的对方"

    @classmethod
    def tick(cls, c, wid, at, *, adjudication_due=False):
        from world_engine.actions import ActionService
        from world_engine.activity_tasks import TaskService
        from world_engine.domain import ActionProposal, ActionType
        from world_engine.economy import EconomyService
        from world_engine.npc_agency import NpcAgencyService

        slot = at.strftime("%Y-%m-%dT%H")
        from world_engine.journeys import JourneyService
        from world_engine.group_journeys import GroupJourneyService
        from world_engine.overnight import OvernightService
        from world_engine.separate_stays import SeparateStayService
        from world_engine.lodging_plans import LodgingPlanService
        JourneyService.maintain(c,wid,at)
        GroupJourneyService.maintain(c,wid,at)
        from world_engine.rides import RideService
        RideService.maintain(c,wid,at)
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
            from world_engine.families import FamilyService

            if RideService.aboard(c, npc["id"]):
                continue
            if FamilyService.is_dependent(c,npc["id"],at):
                continue
            if GroupJourneyService.perform(c,npc,at):
                # 哥哥，本人已确认的集合、带路及停留优先；暂停后仍会处理真实生活需要。
                continue
            if JourneyService.active(c,wid,npc["id"]):
                # 哥哥，只有已取得本人跟随选择的 NPC 领队会从这里实际发起下一程。
                JourneyService.perform(c,npc,at)
                continue
            # 哥哥，已同意的短时同行保留当前安排，结束后再恢复自主生活。
            if c.execute("SELECT 1 FROM companion_sessions WHERE companion_id=? AND status='active'",
                         (npc['id'],)).fetchone():
                continue
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
            from world_engine.shared_plans import SharedPlanService
            has_shared_plan=bool(c.execute("SELECT 1 FROM shared_preparation_plans WHERE world_id=? AND status='active' AND (proposer_id=? OR recipient_id=?)",(wid,npc["id"],npc["id"])).fetchone())
            has_overnight = bool(c.execute(
                "SELECT 1 FROM journey_overnight_stays WHERE world_id=? AND status='accepted' "
                "AND (proposer_id=? OR recipient_id=?)", (wid, npc["id"], npc["id"]),
            ).fetchone())
            has_overnight = has_overnight or bool(c.execute(
                "SELECT 1 FROM journey_separate_stays WHERE world_id=? "
                "AND (status IN ('accepted','active') OR (status='completed' "
                "AND (reunion_status='waiting' OR (reunion_status='met' AND reunion_ends_at>? "
                "AND reunion_wait_ended_at IS NULL)))) "
                "AND (proposer_id=? OR recipient_id=?)", (wid, to_iso(at), npc["id"], npc["id"]),
            ).fetchone())
            if has_goals:
                NpcGoalService.refresh(c,npc,at)
            changed=changed or bool(routine and routine["changed"])
            next_check = from_iso(state["next_world_time"]) if state["next_world_time"] else at
            hungry_child=FamilyService.hungry_dependent(c,npc,at)
            critical = npc["health"] < 40 or npc["satiety"] <= 10 or npc["energy"] <= 10 or bool(hungry_child)
            appointment_place = by_id.get(plan["contract"]["location_id"]) if plan and plan.get("appointment") else None
            meeting_partner = cls._visible_appointment_partner(c, npc, plan, at, appointment_place) if plan and not critical else None
            has_lodging_plan = bool(c.execute(
                "SELECT 1 FROM journey_lodging_plans WHERE world_id=? AND status='accepted' "
                "AND (proposer_id=? OR recipient_id=?)", (wid, npc["id"], npc["id"]),
            ).fetchone())
            # 哥哥，裁判延迟不能让需要用饭或有危急身体状态的人一直停止自主生活。
            if (adjudication_due and (routine is None or routine["slot"] is None) and not has_goals and not hungry_child and not has_shared_plan and not has_overnight and not has_lodging_plan and not critical and npc["satiety"] > 30 and not meeting_partner) or (
                state["last_slot"] == slot and not changed and at < next_check and not critical and not meeting_partner
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
            lodging_intention = None
            c.execute("SAVEPOINT npc_daily_action")
            try:
                EconomyService.collect_outputs(c, npc, at, defer_agency=True)
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
                sleep_time = cls.is_sleep_time(npc, at)

                def near(loc, actor=npc):
                    return (
                        great_circle_distance_km(
                            actor["longitude"], actor["latitude"], loc["longitude"], loc["latitude"]
                        )
                        <= 0.1
                    )

                remaining = ScheduleService.planned_minutes(plan, at)
                night_remaining=OvernightService.planned_minutes(c,npc,at)
                if night_remaining is not None:remaining=night_remaining if remaining is None else min(remaining,night_remaining)
                if routine and routine["next_boundary"]>at:
                    until_boundary=max(0,int((routine["next_boundary"]-at).total_seconds()//60))
                    remaining=until_boundary if remaining is None else min(remaining,until_boundary)
                urgent_care = npc["health"] < 40 or npc["satiety"] <= 10 or npc["energy"] <= 10
                if plan and plan["due"] and not urgent_care and plan["contract"]["is_active"]:
                    RoutineService.defer(c,routine,at,"优先处理已经确认的见面约定")
                    destination = by_id.get(plan["contract"]["location_id"])
                    date=plan.get("appointment")
                    if date and date["room_id"]:
                        from world_engine.appointment_rooms import AppointmentRoomService
                        intention=AppointmentRoomService.approach(c,npc,date,at,destination)
                    elif meeting_partner:
                        intention = cls._approach_appointment_partner(c, npc, meeting_partner, date, at)
                    else:
                        intention = (
                        cls._travel(c, npc, at, destination)
                        if npc["current_room_id"] or not near(destination)
                        else "已到约定地点，等候对方"
                        )
                elif npc["satiety"] <= 30:
                    RoutineService.defer(c,routine,at,"先解决用饭需要，再继续日常安排")
                    agency_intention = NpcAgencyService.tick(c, wid, npc["id"], at)
                    if agency_intention:
                        intention = agency_intention
                    else:
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
                    if intention == "处理自己的事务":
                        if EconomyService.buy_food(c, npc, at):
                            intention = "买到了真实食物"
                        elif EconomyService.harvest(c, npc, at, food_only=True):
                            intention = "收取可以食用的资源"
                        else:
                            obstacle = EconomyService.supply_obstacle(c, npc, at, food_only=True)
                            if obstacle.kind == "insufficient_funds":
                                intention = (
                                    cls._rest_at_home(c, npc, at, 120)
                                    if npc["energy"] < 20
                                    else cls._find_work(c, npc, at, local_places, plan)
                                )
                        food_places = [
                            by_id[place_id]
                            for place_id in cls.known_food_places(
                                c, npc, at, [loc["id"] for loc in local_places]
                            )
                        ]
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
                            elif npc["energy"] < 20:
                                intention = cls._rest_at_home(c, npc, at, 120)
                        if not food_places and intention == "处理自己的事务":
                            familiar = cls.familiar_place_to_search(c, npc, at, by_id)
                            if familiar:
                                from world_engine.movement import RouteUnavailable

                                # 哥哥，仅撤销本次不可达移动，不抹去此前真实的自救判断。
                                c.execute("SAVEPOINT npc_familiar_search")
                                try:
                                    cls._travel(c, npc, at, familiar)
                                except RouteUnavailable as exc:
                                    c.execute("ROLLBACK TO SAVEPOINT npc_familiar_search")
                                    c.execute("RELEASE SAVEPOINT npc_familiar_search")
                                    intention = f"暂时无法前往{familiar['name']}：{exc}"
                                except BaseException:
                                    c.execute("ROLLBACK TO SAVEPOINT npc_familiar_search")
                                    c.execute("RELEASE SAVEPOINT npc_familiar_search")
                                    raise
                                else:
                                    c.execute("RELEASE SAVEPOINT npc_familiar_search")
                                    intention = "回到自己熟悉的地点看看"
                        if intention == "处理自己的事务":
                            intention = {
                                "no_seller": "附近没有可交易的食物卖家，暂待其他食物来源",
                                "out_of_stock": "附近卖家没有可用食物，暂待补货或其他来源",
                                "inventory_full": "背包没有空间容纳食物，需先腾出空间",
                                "actor_unavailable": "当前无法采购食物，稍后重新评估",
                            }.get(obstacle.kind, "暂待其他食物来源")
                elif hungry_child and npc["energy"]>25 and npc["health"]>=40:
                    RoutineService.defer(c,routine,at,"为已确认的家庭照护准备实际食物")
                    intention=FamilyService.care_intention(c,npc,at,local_places,plan)
                elif npc["health"] < 80 and (care_intention := NpcAgencyService.tick(c, wid, npc["id"], at)):
                    intention = care_intention
                    RoutineService.defer(c, routine, at, "先处理身体伤势")
                elif (lodging_intention := LodgingPlanService.perform(c, npc, at)):
                    intention = lodging_intention
                    RoutineService.defer(c, routine, at, "处理本人已明确同意的限额订房筹备")
                    npc = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
                elif (separate_intention:=SeparateStayService.perform(c,npc,at)):
                    intention=separate_intention
                    # 哥哥，本人进门或开始休息后要用新状态记账，不替玩家采取任何行动。
                    npc=c.execute("SELECT * FROM characters WHERE id=?",(npc["id"],)).fetchone()
                    RoutineService.defer(c,routine,at,"处理本人已独立确认的分房休息与会合")
                elif (night_intention:=OvernightService.perform(c,npc,at)):
                    intention=night_intention
                    RoutineService.defer(c,routine,at,"处理本人已独立确认的长时住宿")
                elif has_shared_plan and (shared_intention:=SharedPlanService.perform(c,npc,at,local_places,plan,routine)):
                    intention=shared_intention
                elif (journey_intention:=JourneyService.perform(c,npc,at)):
                    intention=journey_intention
                    RoutineService.defer(c,routine,at,"前往本人已明确答应的共同旅行集合处")
                elif npc["energy"] > 25 and not sleep_time and c.execute(
                    "SELECT 1 FROM research_programs WHERE character_id=? AND status='active'",
                    (npc["id"],),
                ).fetchone():
                    intention = "继续自己已经采纳的研究程序"
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
                elif agency_intention := NpcAgencyService.tick(c, wid, npc["id"], at):
                    intention = agency_intention
                    RoutineService.defer(c, routine, at, "自主目标正在推进")
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
                if has_shared_plan:
                    shared_retry=c.execute("SELECT MIN(r.retry_at) FROM shared_preparation_roles r JOIN shared_preparation_plans p ON p.id=r.plan_id WHERE p.world_id=? AND p.status='active' AND r.character_id=? AND r.retry_at>?",(wid,npc["id"],to_iso(at))).fetchone()[0]
                    if shared_retry:next_time=min(next_time,from_iso(shared_retry))
                if has_lodging_plan:
                    lodging_retry = c.execute(
                        "SELECT MIN(r.retry_at) FROM journey_lodging_roles r "
                        "JOIN journey_lodging_plans p ON p.id=r.plan_id "
                        "WHERE p.world_id=? AND p.status='accepted' AND r.character_id=? AND r.retry_at>?",
                        (wid, npc["id"], to_iso(at)),
                    ).fetchone()[0]
                    if lodging_retry:
                        next_time = min(next_time, from_iso(lodging_retry))
                    if lodging_intention is not None and intention == lodging_intention:
                        next_time = min(next_time, at + timedelta(minutes=1))
                if routine:
                    if routine["next_boundary"]>at:
                        next_time=min(next_time,routine["next_boundary"])
                    if routine.get("occurrence"):
                        retry=c.execute("SELECT next_attempt_world_time FROM npc_routine_occurrences WHERE id=?",(routine["occurrence"]["id"],)).fetchone()
                        if retry and retry[0] and from_iso(retry[0])>at:
                            next_time=min(next_time,from_iso(retry[0]))
                if plan and plan["depart_at"] > at:
                    next_time = min(next_time, plan["depart_at"])
                night_remaining=OvernightService.planned_minutes(c,npc,at)
                if night_remaining is not None:next_time=min(next_time,at+timedelta(minutes=max(1,night_remaining)))
                if intention in {"离开房间", "经过住所入口", "经过约会房间入口", "等候约会接待者开门与接待", "经过本人分房住宿入口"}:
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
                current_activity = LifeActivityService.running(c, npc["id"])
                current_movement = c.execute(
                    "SELECT status,destination_location_id FROM character_movements "
                    "WHERE character_id=? AND status='moving'",
                    (npc["id"],),
                ).fetchone()
                LOGGER.info(
                    "NPC自主生活行动 world_id=%s character_id=%s intention=%s "
                    "activity=%s movement_destination=%s",
                    wid,
                    npc["id"],
                    diagnostic_json(intention),
                    current_activity["kind"] if current_activity else None,
                    current_movement["destination_location_id"] if current_movement else None,
                )
            except ValueError as exc:
                c.execute("ROLLBACK TO SAVEPOINT npc_daily_action")
                c.execute("RELEASE SAVEPOINT npc_daily_action")
                c.execute(
                    "UPDATE npc_daily_states SET last_slot=?,intention=?,last_error=? WHERE character_id=?",
                    (slot, "当前条件不足，暂缓计划", str(exc)[:300], npc["id"]),
                )
                LOGGER.warning(
                    "NPC自主生活行动受阻 world_id=%s character_id=%s reason=%s",
                    wid,
                    npc["id"],
                    diagnostic_json(str(exc)[:300]),
                )
