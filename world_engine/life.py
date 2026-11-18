"""持续生活活动与眼前物品；所有结果依据存档中的真实状态。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM
from world_engine.repository import from_iso, to_iso, utc_now


ABOARD_LIFE_SCHEMA = """
CREATE TABLE IF NOT EXISTS aboard_life_activities (
    activity_id TEXT PRIMARY KEY REFERENCES character_life_activities(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id),
    ride_id TEXT NOT NULL REFERENCES vehicle_rides(id),
    vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
    movement_id TEXT REFERENCES character_movements(id),
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    space_layer TEXT NOT NULL,
    current_location_id TEXT REFERENCES locations(id),
    started_world_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aboard_life_ride ON aboard_life_activities(ride_id,character_id);
"""


class LifeActivityError(ValueError):
    pass


def parse_life_activity(text: str) -> tuple[str, int] | None:
    """识别明确的单一休息/等待指令；复杂描述留给原有行动路径。"""
    match = re.fullmatch(
        r"(?:我)?(休息(?:一下|一会儿|片刻)?|原地休息|睡觉|睡一觉|等待|原地等待)"
        r"\s*(?:(\d+)\s*(分钟|小时))?\s*[。！!]?",
        text.strip(),
    )
    if not match:
        return None
    kind = "wait" if "等待" in match[1] else "rest"
    duration = (
        int(match[2]) * (60 if match[3] == "小时" else 1)
        if match[2]
        else (30 if kind == "wait" else 60)
    )
    return kind, duration


class LifeActivityService:
    @staticmethod
    def install_aboard(connection):
        # 哥哥，逐条建表保留调用方事务，旧活动不能补造在车记录。
        for statement in ABOARD_LIFE_SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _aboard_stationary(connection, actor, world_time):
        """只认本人有来源的在车绑定及载具真实静止状态。"""
        from world_engine.rides import RideService

        ride = RideService.aboard(connection, actor["id"])
        if ride is None:
            return None
        member = connection.execute(
            "SELECT * FROM vehicle_ride_members WHERE ride_id=? AND character_id=?",
            (ride["id"], actor["id"]),
        ).fetchone()
        state = connection.execute(
            "SELECT * FROM vehicle_states WHERE vehicle_id=? AND world_id=?",
            (ride["vehicle_id"], actor["world_id"]),
        ).fetchone()
        if (ride["world_id"] != actor["world_id"] or ride["status"] != "blocked" or state is None
                or state["status"] != "blocked" or member is None or not member["live"]
                or member["decision"] != "accepted" or not member["boarded_at"] or member["disembarked_at"]
                or not RideService._accepted(connection, ride, world_time)
                or not RideService._source(connection, ride, member["board_event_id"], actor["id"], "boarded", world_time, terms=True)):
            raise LifeActivityError("只有本人真实在静止挂起的原载具上，才能在车内休息或等待")
        if (actor["space_layer"] != state["space_layer"] or actor["current_room_id"] or actor["current_fixture_id"]
                or actor["current_location_id"] != state["current_location_id"]
                or actor["longitude"] is None or actor["latitude"] is None
                or great_circle_distance_km(actor["longitude"], actor["latitude"], state["longitude"], state["latitude"]) > 0.00001):
            raise LifeActivityError("本人和载具的实际位置或空间层不一致，不能开始车内活动")
        if connection.execute(
            "SELECT 1 FROM character_movements WHERE world_id=? AND vehicle_id=? AND status='moving'",
            (actor["world_id"], ride["vehicle_id"]),
        ).fetchone():
            raise LifeActivityError("载具仍有真实移动，不能在原位休息或等待")
        return ride, state

    @staticmethod
    def running(connection: sqlite3.Connection, character_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM character_life_activities WHERE character_id=? AND status='running'",
            (character_id,),
        ).fetchone()

    @classmethod
    def assert_available(cls, connection, character_id: str, *, talking: bool = False) -> None:
        active = cls.running(connection, character_id)
        if active and not (talking and active["kind"] == "wait"):
            raise LifeActivityError("请先结束当前活动，再开始新的行动")

    @classmethod
    def start(cls, connection, actor, world_time: datetime, kind: str, minutes: int) -> str:
        from world_engine.rides import RideService
        # 哥哥，重新读本人真实状态，不能以旧快照或前端字段替代当前事实。
        actor = connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (actor["id"], actor["world_id"])).fetchone()
        if actor is None:
            raise LifeActivityError("人物不存在或不属于当前世界")
        aboard = RideService.aboard(connection, actor["id"])
        if aboard and kind not in {"rest", "wait"}:
            raise LifeActivityError("本人仍在载具上，只有静止挂起时可自行休息或等待")
        binding = cls._aboard_stationary(connection, actor, world_time) if aboard else None
        cls.assert_available(connection, actor["id"])
        if (
            kind not in {"rest", "wait", "work", "craft", "repair", "map_review", "research"}
            or isinstance(minutes, bool)
            or not isinstance(minutes, int)
        ):
            raise LifeActivityError("活动类型或持续时间无效")
        if not 1 <= minutes <= 720:
            raise LifeActivityError("每次活动需要持续 1 至 720 个世界分钟")
        if actor["health"] <= 0:
            raise LifeActivityError("当前身体状态无法开始活动")
        from world_engine.player_steering import PlayerSteeringService

        if PlayerSteeringService.active(connection, actor["id"]):
            raise LifeActivityError("请先松开方向键，再开始原地活动")
        if connection.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (actor["id"],),
        ).fetchone():
            raise LifeActivityError("请先停下或抵达目的地，再原地休息或等待")
        activity_id = str(uuid4())
        if binding:
            connection.execute("SAVEPOINT aboard_life_start")
        try:
            connection.execute(
            """INSERT INTO character_life_activities(
                id,world_id,character_id,kind,status,location_id,longitude,latitude,
                initial_health,started_world_time,ends_world_time,last_processed_world_time,created_at
            ) VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?)""",
            (
                activity_id,
                actor["world_id"],
                actor["id"],
                kind,
                binding[1]["current_location_id"] if binding else actor["current_location_id"] or actor["location_id"],
                actor["longitude"],
                actor["latitude"],
                actor["health"],
                to_iso(world_time),
                to_iso(world_time + timedelta(minutes=minutes)),
                to_iso(world_time),
                to_iso(utc_now()),
            ),
            )
            if binding:
                ride, state = binding
                connection.execute(
                    "INSERT INTO aboard_life_activities(activity_id,world_id,character_id,ride_id,vehicle_id,movement_id,longitude,latitude,space_layer,current_location_id,started_world_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (activity_id, actor["world_id"], actor["id"], ride["id"], ride["vehicle_id"], ride["movement_id"],
                     state["longitude"], state["latitude"], state["space_layer"], state["current_location_id"], to_iso(world_time)),
                )
        except Exception:
            if binding:
                connection.execute("ROLLBACK TO aboard_life_start")
                connection.execute("RELEASE aboard_life_start")
            raise
        if binding:
            connection.execute("RELEASE aboard_life_start")
        return activity_id

    @staticmethod
    def view(row, world_time: datetime) -> dict[str, object]:
        start, end = from_iso(row["started_world_time"]), from_iso(row["ends_world_time"])
        effective = (
            from_iso(row["finished_world_time"]) if row["finished_world_time"] else world_time
        )
        elapsed = max(0.0, (min(effective, end) - start).total_seconds())
        total = (end - start).total_seconds()
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "started_world_time": row["started_world_time"],
            "ends_world_time": row["ends_world_time"],
            "finished_world_time": row["finished_world_time"],
            "duration_minutes": round(total / 60),
            "elapsed_minutes": round(elapsed / 60, 2),
            "progress": min(1.0, elapsed / total),
            "reason": row["reason"],
        }

    @classmethod
    def finish(cls, connection, row, at: datetime, status: str, reason: str) -> None:
        from world_engine.actions import ActionService

        current = connection.execute(
            "SELECT status FROM character_life_activities WHERE id=?", (row["id"],),
        ).fetchone()
        if current is None or current["status"] != "running":
            return
        actor = connection.execute(
            "SELECT name FROM characters WHERE id=?",
            (row["character_id"],),
        ).fetchone()
        name = {
            "rest": "休息",
            "wait": "等待",
            "work": "工作",
            "craft": "制作",
            "repair": "修理",
            "map_review": "地图核对",
            "research": "研究",
        }[row["kind"]]
        summary = (
            f"{actor['name']}的{name}{'结束了' if status == 'completed' else '已停止'}。{reason}"
        )
        aboard = connection.execute(
            "SELECT ride_id,vehicle_id FROM aboard_life_activities WHERE activity_id=?",
            (row["id"],),
        ).fetchone()
        event_id = ActionService._record_event(
            connection,
            world_id=row["world_id"],
            tick_id=f"life:{row['id']}:{status}",
            occurred_at=at,
            event_type="action.work"
            if row["kind"] == "work" and status == "completed"
            else f"action.life_{status}",
            actor_id=row["character_id"],
            target_id=None,
            location_id=row["location_id"],
            summary=summary,
            payload={
                "activity_id": row["id"], "kind": row["kind"], "reason": reason,
                **({"aboard_activity": {"ride_id": aboard["ride_id"], "vehicle_id": aboard["vehicle_id"]}} if aboard else {}),
            },
        )
        if row["kind"] in {"work", "craft", "repair", "map_review"}:
            from world_engine.activity_tasks import TaskService

            result = TaskService.finish(connection, row, status, event_id, at)
            if result.get("check"):
                label = {"success": "检定成功", "partial": "部分成功", "failure": "检定失败"}[
                    result["check"]["outcome"]
                ]
                reason = label + "。" + result.get("description", "")
                summary = f"{actor['name']}的{name}尝试结束：{reason}"

            summary += (
                f" 已结算{result['payment']}枚货币。"
                if result.get("payment")
                else (
                    " 成果或退还材料可在原活动地点领取。"
                    if row["kind"] not in {"work", "map_review"}
                    else " 结果已保存在行动工作记录中。"
                    if row["kind"] == "map_review"
                    else " 未完成工作不发放报酬。"
                )
            )
            connection.execute(
                "UPDATE world_events SET summary=?,payload_json=? WHERE id=?",
                (
                    summary,
                    json.dumps(
                        {
                            **json.loads(connection.execute("SELECT payload_json FROM world_events WHERE id=?", (event_id,)).fetchone()[0]),
                            "activity_id": row["id"],
                            "result": result,
                            "work_completed": row["kind"] == "work" and status == "completed",
                        },
                        ensure_ascii=False,
                    ),
                    event_id,
                ),
            )
        connection.execute("UPDATE player_notifications SET title=? WHERE event_id=?", (summary[:180],event_id))
        from world_engine.event_history import EventHistoryService
        EventHistoryService.record(connection,event_id)
        connection.execute("INSERT OR IGNORE INTO player_notifications(id,world_id,recipient_id,event_id,title,created_at) SELECT ?,world_id,id,?,?,? FROM characters WHERE id=? AND is_player=1",(str(uuid4()),event_id,summary[:180],to_iso(at),row["character_id"]))
        connection.execute(
            """UPDATE character_life_activities SET status=?,finished_world_time=?,reason=?,
                finish_event_id=? WHERE id=? AND status='running'""",
            (status, to_iso(at), reason, event_id, row["id"]),
        )
        if row["kind"] == "research":
            from world_engine.principle_cycles import settle_activity

            settle_activity(connection, row, at, status)
        from world_engine.action_results import finish_execution

        finish_execution(
            connection, kind="activity", execution_id=row["id"], event_id=event_id,
            world_id=row["world_id"], actor_id=row["character_id"], at=at, status=status,
            source_event_id=row["source_event_id"],
        )
        ActionService._record_memory(
            connection,
            world_id=row["world_id"],
            character_id=row["character_id"],
            event_id=event_id,
            memory_type="experienced",
            summary=summary,
            importance=2,
        )

    @classmethod
    def interrupt(cls, connection, character_id: str, at: datetime, reason: str) -> None:
        row = cls.running(connection, character_id)
        if row:
            cls.finish(connection, row, at, "interrupted", reason)

    @classmethod
    def advance(cls, connection, world_id: str, at: datetime, heartbeat_id: str) -> int:
        from world_engine.overnight import OvernightService
        OvernightService.maintain(connection,world_id,at,complete=False)
        rows = connection.execute(
            "SELECT * FROM character_life_activities WHERE world_id=? AND status='running'",
            (world_id,),
        ).fetchall()
        updates = 0
        for row in rows:
            actor = connection.execute(
                "SELECT * FROM characters WHERE id=?",
                (row["character_id"],),
            ).fetchone()
            last = from_iso(row["last_processed_world_time"])
            binding = connection.execute("SELECT * FROM aboard_life_activities WHERE activity_id=?", (row["id"],)).fetchone()
            if binding:
                try:
                    actual = cls._aboard_stationary(connection, actor, at)
                    if (actual is None or actual[0]["id"] != binding["ride_id"]
                            or actual[0]["vehicle_id"] != binding["vehicle_id"]
                            or actual[0]["movement_id"] != binding["movement_id"]
                            or actual[1]["space_layer"] != binding["space_layer"]
                            or actual[1]["current_location_id"] != binding["current_location_id"]
                            or great_circle_distance_km(actual[1]["longitude"], actual[1]["latitude"], binding["longitude"], binding["latitude"]) > 0.00001):
                        raise LifeActivityError("原载具或位置已经变化")
                except (ValueError, LookupError):
                    cls.finish(connection, row, max(last, at), "interrupted", "原载具、在车绑定或静止位置已经变化。")
                    updates += 1
                    continue
            moved = (
                great_circle_distance_km(
                    actor["longitude"],
                    actor["latitude"],
                    row["longitude"],
                    row["latitude"],
                )
                > 0.001
            )
            task = connection.execute(
                "SELECT room_id FROM activity_task_details WHERE activity_id=?", (row["id"],)
            ).fetchone()
            if (
                actor["health"] < row["initial_health"]
                or moved
                or (task and task["room_id"] != actor["current_room_id"])
            ):
                cls.finish(
                    connection, row, max(last, at), "interrupted", "身体状况或所在位置发生变化。"
                )
                updates += 1
                continue
            end = from_iso(row["ends_world_time"])
            effective = min(at, end)
            spoiled_material = False
            if task:
                deadline = connection.execute(
                    "SELECT MIN(spoils_world_time) FROM item_instances WHERE container_type='activity_escrow' "
                    "AND container_id=? AND spoils_world_time IS NOT NULL", (row["id"],)
                ).fetchone()[0]
                if deadline and from_iso(deadline) <= effective:
                    effective = max(last, from_iso(deadline))
                    spoiled_material = True
            if spoiled_material and effective <= last:
                cls.finish(connection, row, last, "interrupted", "预留食材已变质，工序停止。")
                updates += 1
                continue
            if effective <= last:
                continue
            if row["kind"] == "research":
                from world_engine.autonomous_research import ResearchService

                able = ResearchService.advance_work(connection, row, actor, effective, heartbeat_id)
                connection.execute(
                    "UPDATE character_life_activities SET last_processed_world_time=? WHERE id=?",
                    (to_iso(effective), row["id"]),
                )
                if not able:
                    cls.finish(connection, row, effective, "interrupted", "身体需要先休息或用饭，研究暂缓。")
                elif at >= end:
                    cls.finish(connection, row, end, "completed", "本段研究劳动已实际完成。")
                updates += 1
                continue
            if row["kind"] in {"work", "craft", "repair", "map_review"}:
                from world_engine.activity_tasks import TaskService

                able = TaskService.progress(connection, row, actor, effective, heartbeat_id)
                connection.execute(
                    "UPDATE character_life_activities SET last_processed_world_time=? WHERE id=?",
                    (to_iso(effective), row["id"]),
                )
                if spoiled_material:
                    cls.finish(connection, row, effective, "interrupted", "预留食材已变质，工序停止。")
                elif not able:
                    cls.finish(connection, row, effective, "interrupted", "精力不足，活动中断。")
                elif at >= end:
                    cls.finish(connection, row, end, "completed", "预定活动已经完成。")
                updates += 1
                continue
            elapsed = (effective - from_iso(row["started_world_time"])).total_seconds()
            # 额外休息收益每小时五点，零头累计；自然恢复仍由统一时钟独立记录。
            steps = int(elapsed / 720) if row["kind"] == "rest" else 0
            gain = max(0, steps - row["energy_steps"])
            energy = min(100, actor["energy"] + gain)
            if energy != actor["energy"]:
                connection.execute(
                    "UPDATE characters SET energy=?,updated_at=? WHERE id=?",
                    (energy, to_iso(utc_now()), actor["id"]),
                )
                connection.execute(
                    """INSERT INTO character_state_updates(
                        id,world_id,heartbeat_id,character_id,world_time_before,world_time_after,
                        changes_json,cause,created_at
                    ) VALUES (?,?,?,?,?,?,?,'rest_time_passage',?)""",
                    (
                        str(uuid4()),
                        world_id,
                        heartbeat_id,
                        actor["id"],
                        to_iso(last),
                        to_iso(effective),
                        json.dumps(
                            {
                                "energy": {
                                    "before": actor["energy"],
                                    "after": energy,
                                    "delta": energy - actor["energy"],
                                }
                            }
                        ),
                        to_iso(utc_now()),
                    ),
                )
            connection.execute(
                "UPDATE character_life_activities SET energy_steps=?,last_processed_world_time=? "
                "WHERE id=?",
                (steps, to_iso(effective), row["id"]),
            )
            if at >= end:
                cls.finish(connection, row, end, "completed", "预定的时间已经过去。")
            updates += 1
        from world_engine.shared_life import SharedLifeService

        SharedLifeService.maintain(connection, world_id, at)
        from world_engine.appointments import AppointmentService

        updates += AppointmentService.maintain(connection, world_id, at)
        from world_engine.shared_plans import SharedPlanService
        updates += SharedPlanService.maintain(connection,world_id,at)
        from world_engine.journeys import JourneyService
        JourneyService.maintain(connection,world_id,at)
        OvernightService.maintain(connection,world_id,at)
        return updates


class LifeSceneService:
    @staticmethod
    def ground_location(connection, actor) -> sqlite3.Row | None:
        location_id = actor["current_location_id"] or actor["location_id"]
        if actor["current_room_id"]:
            room = connection.execute(
                "SELECT location_id FROM life_rooms WHERE id=? AND world_id=?",
                (actor["current_room_id"], actor["world_id"]),
            ).fetchone()
            location_id = room["location_id"] if room else None
        location = connection.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (location_id, actor["world_id"]),
        ).fetchone()
        near = (
            location is not None
            and great_circle_distance_km(
                actor["longitude"],
                actor["latitude"],
                location["longitude"],
                location["latitude"],
            )
            <= VISIBLE_PERSON_RADIUS_KM
        )
        return location if near else None

    @classmethod
    def accessible_items(cls, connection, actor) -> list[sqlite3.Row]:
        location = cls.ground_location(connection, actor)
        room_id = actor["current_room_id"]
        return connection.execute(
            """SELECT i.*,t.name,t.category,t.slot_size,t.stack_limit
                FROM item_instances i JOIN item_types t ON t.id=i.item_type_id
                WHERE i.world_id=? AND i.quantity>0 AND (
                    (i.container_type='character_inventory' AND i.container_id=?) OR
                    (i.container_type=? AND i.container_id=?)
                ) ORDER BY i.container_type,t.name,i.id""",
            (
                actor["world_id"],
                actor["id"],
                "room_ground" if room_id else "location_ground",
                (room_id or location["id"]) if location else "",
            ),
        ).fetchall()

    @classmethod
    def scene(cls, connection, actor, at: datetime) -> dict[str, object]:
        active = LifeActivityService.running(connection, actor["id"])
        recent = connection.execute(
            "SELECT * FROM character_life_activities WHERE character_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (actor["id"],),
        ).fetchone()
        items = []
        near = cls.ground_location(connection, actor) is not None
        from world_engine.food import FoodService
        for row in cls.accessible_items(connection, actor):
            ground = row["container_type"] in {"location_ground", "room_ground"}
            owned = row["owner_character_id"] == actor["id"] or (
                ground and row["owner_character_id"] is None
            )
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "quantity": row["quantity"],
                    "condition": row["condition"],
                    "category": row["category"],
                    "freshness": FoodService.view(row, at) if row["category"] == "food" else None,
                    "place": "ground" if ground else "bag",
                    "ownership": "unowned"
                    if row["owner_character_id"] is None
                    else ("self" if row["owner_character_id"] == actor["id"] else "other"),
                    "actions": ["inspect"]
                    + (["pickup" if ground else "drop"] if owned and near else []),
                }
            )
        from world_engine.action_checks import CheckService
        from world_engine.activity_tasks import TaskService
        from world_engine.interiors import InteriorService

        workplace = cls.ground_location(connection, actor)
        account = connection.execute("SELECT wage FROM workplace_accounts WHERE location_id=? AND world_id=?",
                                     (workplace["id"], actor["world_id"])).fetchone() if workplace else None
        return {
            "work_payment": None if actor["current_room_id"] else account["wage"] if account else 9 if workplace and workplace["kind"] == "workplace" else None,
            "world_time": to_iso(at),
            "food_discomfort": FoodService.discomfort(connection, actor["id"], at),
            "items": items,
            "activity": TaskService.view(connection, active, at) if active else None,
            "last_activity": TaskService.view(connection, recent, at) if recent else None,
            "interior": InteriorService.scene(connection, actor),
            "recipes": TaskService.available(connection, actor),
            "map_records": [
                dict(row)
                for row in connection.execute(
                    "SELECT id,title,location_id,created_at FROM player_activity_records WHERE world_id=? "
                    "AND player_id=? AND step_key IN ('road_notes','field_notes') AND status='completed' "
                    "ORDER BY created_at DESC LIMIT 30",
                    (actor["world_id"], actor["id"]),
                )
            ],
            "checks": CheckService.list_for(connection, actor["world_id"], actor["id"]),
            "pending_outputs": [
                {**dict(row), "freshness": FoodService.view(row, at)}
                for row in connection.execute(
                    "SELECT i.id,t.name,i.quantity,i.condition,i.fresh_until_world_time,i.spoils_world_time,a.location_id,d.room_id FROM item_instances i "
                    "JOIN item_types t ON t.id=i.item_type_id "
                    "JOIN character_life_activities a ON a.id=i.container_id "
                    "JOIN activity_task_details d ON d.activity_id=a.id "
                    "WHERE i.world_id=? AND i.owner_character_id=? "
                    "AND i.container_type='activity_output'",
                    (actor["world_id"], actor["id"]),
                )
            ],
        }
