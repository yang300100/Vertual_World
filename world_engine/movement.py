from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.contracts import ContractService
from world_engine.domain import MovementState
from world_engine.geo import great_circle_distance_km, nearby_coordinate_bounds
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now
from world_engine.routing import RoutePlan, RoutePlanner
from world_engine.spatial import SpatialContextService

ENCOUNTER_RADIUS_KM = 3.0


class RouteUnavailable(ValueError):
    """哥哥，已审核地形无法规划路线时保留可识别的失败原因。"""


class RouteDataInvalid(ValueError):
    """哥哥，导航路线损坏时必须停在真实位置，不能猜成直线。"""


def assert_vehicle_fuel_accounted(connection, vehicle) -> None:
    """哥哥，已有燃料定义但尚无消耗结算时，不能让旧载具免费启用。"""
    if connection.execute(
        "SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=? AND world_id=?",
        (vehicle["id"], vehicle["world_id"]),
    ).fetchone():
        raise ValueError("该载具已登记有限燃料，行驶消耗结算尚未启用，不能免费出发")
    try:
        metadata = json.loads(vehicle["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        # 哥哥，旧数据无法解析时沿用原处理；不把无关内容猜成燃料。
        return
    if isinstance(metadata, dict) and any("fuel" in str(key).lower() or "燃料" in str(key) for key in metadata):
        raise ValueError("该载具有有限燃料定义，尚无对应消耗结算，不能免费出发")


def interpolate_coordinate(
    origin_longitude: float,
    origin_latitude: float,
    destination_longitude: float,
    destination_latitude: float,
    progress: float,
) -> tuple[float, float]:
    """按最短经度方向插值；当前版本为稳定平面插值，后续可替换为测地线。"""
    ratio = max(0.0, min(1.0, progress))
    longitude_delta = destination_longitude - origin_longitude
    if longitude_delta > 180:
        longitude_delta -= 360
    elif longitude_delta < -180:
        longitude_delta += 360
    longitude = origin_longitude + longitude_delta * ratio
    if longitude > 180:
        longitude -= 360
    elif longitude < -180:
        longitude += 360
    latitude = origin_latitude + (destination_latitude - origin_latitude) * ratio
    return longitude, latitude


class MovementService:
    """创建、推进和取消持续移动；模型与前端都不能直接瞬移人物。"""

    def __init__(self) -> None:
        self.spatial = SpatialContextService()
        self.planner = RoutePlanner()

    def start(self, connection, **kwargs) -> MovementState:
        from world_engine.rides import RideService
        RideService.assert_not_aboard(connection, kwargs['character_id'])
        from world_engine.group_journeys import GroupJourneyService
        return GroupJourneyService.start_independent(connection, starter=self._start_existing, **kwargs)

    def _start_existing(self, connection, **kwargs) -> MovementState:
        from world_engine.companions import CompanionService

        actor = connection.execute('SELECT * FROM characters WHERE id=? AND world_id=?',
                                   (kwargs['character_id'], kwargs['world_id'])).fetchone()
        from world_engine.journeys import JourneyService
        journey = JourneyService.active(connection, kwargs['world_id'], kwargs['character_id'])
        predeparture = None if journey else connection.execute(
            "SELECT * FROM joint_journeys WHERE world_id=? AND status='accepted' "
            "AND follow_authorized_by IS NOT NULL AND (proposer_id=? OR recipient_id=?) LIMIT 1",
            (kwargs['world_id'], kwargs['character_id'], kwargs['character_id']),
        ).fetchone()
        if predeparture:
            # 哥哥，首程前自由离开也会撤回跟随；无效移动连撤回一起回滚。
            connection.execute('SAVEPOINT journey_predeparture_independent_move')
            try:
                JourneyService.pause(
                    connection, predeparture, kwargs['world_time'],
                    '一方在首程出发前选择独立移动，原跟随安排须重新确认',
                )
                result = self.start(connection, **kwargs)
                connection.execute('RELEASE journey_predeparture_independent_move')
                return result
            except Exception:
                connection.execute('ROLLBACK TO journey_predeparture_independent_move')
                connection.execute('RELEASE journey_predeparture_independent_move')
                raise
        if journey:
            if actor['is_player']:
                raise ValueError('共同旅行中请从行程开始下一程，或先暂停、退出再自由移动')
            connection.execute('SAVEPOINT journey_independent_departure')
            try:
                JourneyService.pause(connection, journey, kwargs['world_time'], '对方自主选择了另一段行程')
                result = self.start(connection, **kwargs)
                connection.execute('RELEASE journey_independent_departure')
                return result
            except Exception:
                connection.execute('ROLLBACK TO journey_independent_departure')
                connection.execute('RELEASE journey_independent_departure')
                raise
        session = connection.execute(
            "SELECT * FROM companion_sessions WHERE companion_id=? AND status='active'",
            (kwargs['character_id'],),
        ).fetchone()
        if actor and not actor['is_player'] and session:
            # 哥哥，NPC 自己选择新行程时先退出同行；无效行程连退出记录也一起回滚。
            connection.execute('SAVEPOINT companion_independent_departure')
            try:
                CompanionService.end(
                    connection, session, kwargs['world_time'],
                    '对方自行安排了新的行程，结束这段同行',
                )
                movement = self._start_solo(connection, **kwargs)
                connection.execute('RELEASE companion_independent_departure')
                return movement
            except Exception:
                connection.execute('ROLLBACK TO companion_independent_departure')
                connection.execute('RELEASE companion_independent_departure')
                raise
        members = CompanionService.members(connection, actor) if actor and actor['is_player'] else []
        if not members:
            return self._start_solo(connection, **kwargs)
        CompanionService.maintain(connection, kwargs['world_id'], kwargs['world_time'])
        members = CompanionService.members(connection, actor)
        if not members:
            return self._start_solo(connection, **kwargs)
        if kwargs.get('vehicle_id'):
            raise ValueError('同行时需要各自徒步，请先结束同行再使用载具')
        cap = CompanionService.speed_cap(connection, actor, float(actor['movement_speed_kmh']))
        connection.execute('SAVEPOINT companion_departure')
        try:
            movements = [self._start_solo(connection, **kwargs, speed_cap=cap)]
            for npc in members:
                movements.append(self._start_solo(connection, **{**kwargs, 'character_id': npc['id'],
                                                                'vehicle_id': None}, speed_cap=cap))
            duration = max((m.estimated_arrival_world - kwargs['world_time']).total_seconds() for m in movements)
            arrival = kwargs['world_time'] + timedelta(seconds=duration)
            for session in CompanionService.active(connection, actor['id']):
                if arrival > from_iso(session['until_world_time']):
                    raise ValueError('这段路程超过同行剩余时间，请选择近一些的目的地')
            # 每人保留独立起点和路线；放慢较短路线，让哥哥与同伴同时抵达。
            for m in movements:
                ratio = (m.estimated_arrival_world - kwargs['world_time']).total_seconds() / duration
                route = m.route
                if route:
                    route = json.loads(json.dumps(route))
                    for edge in route.get('edges', []):
                        edge['speed_kmh'] *= ratio
                    route['estimated_hours'] = duration / 3600
                connection.execute('UPDATE character_movements SET speed_kmh=?,route_json=?,estimated_arrival_world=? WHERE id=?',
                                   (m.speed_kmh * ratio, json.dumps(route or [], ensure_ascii=False), to_iso(arrival), m.id))
            CompanionService.bind_movements(connection, actor['id'], movements)
            result = WorldRepository.movement_from_row(connection.execute(
                'SELECT * FROM character_movements WHERE id=?', (movements[0].id,)).fetchone())
            connection.execute('RELEASE companion_departure')
            return result
        except Exception:
            connection.execute('ROLLBACK TO companion_departure')
            connection.execute('RELEASE companion_departure')
            raise

    @staticmethod
    def synchronize(connection, movements, at, deadline):
        """哥哥，各人保留真实起点和路线，只放慢较短的一程以同时抵达。"""
        duration=max((m.estimated_arrival_world-at).total_seconds() for m in movements)
        arrival=at+timedelta(seconds=duration)
        if duration<=0 or arrival>deadline:
            raise ValueError('本程无法在共同旅行期限前实际抵达')
        for movement in movements:
            ratio=(movement.estimated_arrival_world-at).total_seconds()/duration
            if ratio<=0:ratio=1
            route=json.loads(json.dumps(movement.route)) if movement.route else None
            if route:
                for edge in route.get('edges',[]):edge['speed_kmh']*=ratio
                route['estimated_hours']=duration/3600
            connection.execute('UPDATE character_movements SET speed_kmh=?,route_json=?,estimated_arrival_world=? WHERE id=?',
                (movement.speed_kmh*ratio,json.dumps(route) if route else '[]',to_iso(arrival),movement.id))

    def _start_solo(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        destination_longitude: float,
        destination_latitude: float,
        vehicle_id: str | None = None,
        world_time: datetime,
        created_at: datetime | None = None,
        record_log: bool = True,
        speed_cap: float | None = None,
        minimum_distance_km: float = 0.05,
        ride_id: str | None = None,
    ) -> MovementState:
        now = created_at or utc_now()
        actor = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (character_id, world_id),
        ).fetchone()
        if actor is None:
            raise LookupError(character_id)
        selected_before_action = vehicle_id or actor["active_vehicle_id"]
        if selected_before_action:
            eligible = ContractService.vehicle_for(connection, world_id, character_id, selected_before_action)
            if eligible is not None:
                # 哥哥，在停止旧方向或改变任何安排前先核验资源，失败不留下副作用。
                assert_vehicle_fuel_accounted(connection, eligible)
        from world_engine.rides import RideService
        ride = RideService.aboard(connection, character_id)
        if ride and (ride_id != ride['id'] or ride['controller_id'] != character_id or ride['status'] != 'boarding'):
            raise ValueError('本人仍在载具上，不能绕过共乘安排独立移动')
        from world_engine.life import LifeActivityService

        LifeActivityService.assert_available(connection, character_id)
        from world_engine.player_steering import PlayerSteeringService

        if PlayerSteeringService.active(connection, character_id):
            PlayerSteeringService.stop(connection, world_id, world_time, "改为前往所选目标。")
        if actor["current_room_id"]:
            raise ValueError("请先从房间离开，再开始室外行程")
        active = connection.execute(
            """
            SELECT id FROM character_movements
            WHERE character_id = ? AND status = 'moving'
            """,
            (character_id,),
        ).fetchone()
        if active is not None:
            raise ValueError("人物已经在移动中，请先取消当前行程")

        longitude = float(destination_longitude)
        latitude = float(destination_latitude)
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("目标经纬度超出世界范围")
        if actor["space_layer"] != "ground":
            from world_engine.wonders import WonderService
            if not WonderService.contains(connection,world_id,actor["space_layer"],longitude,latitude):
                raise ValueError("目标超出本层城市边界，请经通道离城")
        if (
            great_circle_distance_km(
                float(actor["longitude"]), float(actor["latitude"]), longitude, latitude
            )
            < minimum_distance_km
        ):
            raise ValueError("目标位置与当前位置过近")

        selected_vehicle_id = vehicle_id or actor["active_vehicle_id"]
        if selected_vehicle_id and connection.execute('SELECT 1 FROM vehicle_states WHERE vehicle_id=?', (selected_vehicle_id,)).fetchone():
            if not ride or ride_id != ride['id'] or ride['vehicle_id'] != selected_vehicle_id:
                raise ValueError('有真实位置的载具请从共乘约定亲自登乘并由原控制者出发')
        movement_type = actor["movement_type"]
        speed_kmh = float(actor["movement_speed_kmh"])
        vehicle_name = "徒步"
        if selected_vehicle_id:
            vehicle = ContractService.vehicle_for(connection, world_id, character_id, selected_vehicle_id)
            if vehicle is None:
                raise ValueError("所选交通工具不可用或不属于当前人物")
            movement_type = vehicle["movement_type"]
            speed_kmh = float(vehicle["speed_kmh"])
            vehicle_name = vehicle["name"]

        if speed_cap is not None:
            speed_kmh = min(speed_kmh, speed_cap)

        route = self._plan_route(
            connection,
            world_id=world_id,
            actor=actor,
            destination_longitude=longitude,
            destination_latitude=latitude,
            movement_type=movement_type,
            speed_kmh=speed_kmh,
            selected_vehicle_id=selected_vehicle_id,
        )
        route_json = "[]"
        route_distance_km = 0.0
        navigation_dataset_id = None
        replan_reason = None
        if route is not None:
            route_json = json.dumps(route.as_dict(), ensure_ascii=False)
            # 哥哥，预计时刻与实际推进使用同一组已保存段长和速度，避免四舍五入后提前到期。
            total_distance = sum(float(edge["length_km"]) for edge in route.edges)
            route_hours = sum(float(edge["length_km"]) / float(edge["speed_kmh"]) for edge in route.edges)
            estimated_arrival = world_time + timedelta(hours=route_hours)
            route_distance_km = total_distance
            navigation_dataset_id = route.dataset_id
        else:
            total_distance = great_circle_distance_km(
                actor["longitude"], actor["latitude"], longitude, latitude
            )
            if total_distance < minimum_distance_km:
                raise ValueError("目标位置与当前位置过近")
            estimated_arrival = world_time + timedelta(hours=total_distance / speed_kmh)

        destination_location = self._nearest_location(connection, world_id, longitude, latitude, actor["space_layer"])
        initial_encounters = self._nearby_character_ids(
            connection,
            world_id=world_id,
            character_id=character_id,
            longitude=float(actor["longitude"]),
            latitude=float(actor["latitude"]),
        )
        movement_id = str(uuid4())
        if record_log:
            from world_engine.actions import ActionService

            before = ActionService._observed_state(connection, character_id)
        connection.execute(
            """
            INSERT INTO character_movements(
                id, world_id, character_id, status, movement_type, vehicle_id,
                speed_kmh, origin_longitude, origin_latitude,
                destination_longitude, destination_latitude,
                total_distance_km, distance_travelled_km,
                destination_location_id, route_json, route_index, route_distance_km,
                navigation_dataset_id, replan_reason,
                started_at_world, updated_at_world,
                estimated_arrival_world, encountered_character_ids_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'moving', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                movement_id,
                world_id,
                character_id,
                movement_type,
                selected_vehicle_id,
                speed_kmh,
                actor["longitude"],
                actor["latitude"],
                longitude,
                latitude,
                total_distance,
                destination_location["id"] if destination_location else None,
                route_json,
                route_distance_km,
                navigation_dataset_id,
                replan_reason,
                to_iso(world_time),
                to_iso(world_time),
                to_iso(estimated_arrival),
                json.dumps(initial_encounters, ensure_ascii=False),
                to_iso(now),
                to_iso(now),
            ),
        )
        if record_log:
            route_distance = route.distance_km if route is not None else total_distance
            route_hours = route.estimated_hours if route is not None else total_distance / speed_kmh
            self._record_log(
                connection,
                world_id=world_id,
                world_time=world_time,
                event_type="action.route_planned",
                actor_id=character_id,
                location_id=actor["current_location_id"] or actor["location_id"],
                summary=(
                    f"已为{actor['name']}规划{'地形' if route is not None else '直线'}路径："
                    f"{route_distance:.1f}公里，预计{route_hours:.1f}小时。"
                ),
                payload={
                    "movement_id": movement_id,
                    "navigation_dataset_id": route.dataset_id if route is not None else None,
                    "distance_km": route_distance,
                    "estimated_hours": route_hours,
                    "requirements": route.requirements if route is not None else [],
                    "route_available": route is not None,
                },
            )
            event_id = self._record_log(
                connection,
                world_id=world_id,
                world_time=world_time,
                event_type="action.movement_started",
                actor_id=character_id,
                location_id=actor["current_location_id"] or actor["location_id"],
                summary=(
                    f"{actor['name']}以{vehicle_name}开始前往"
                    f"{longitude:.3f}°, {latitude:.3f}°，"
                    f"预计行程{total_distance:.1f}公里。"
                ),
                payload={
                    "movement_id": movement_id,
                    "movement_type": movement_type,
                    "vehicle_id": selected_vehicle_id,
                    "speed_kmh": speed_kmh,
                    "destination_longitude": longitude,
                    "destination_latitude": latitude,
                },
            )
            from world_engine.domain import ActionProposal, ActionType

            ActionService._record_effect(
                connection, world_id, event_id,
                ActionProposal(actor_id=character_id, action=ActionType.TRAVEL,
                               reason="前往已选择的目标坐标"),
                world_time, before, ActionService._observed_state(connection, character_id),
                None, None, "action.movement_started",
            )
        return WorldRepository.movement_from_row(
            connection.execute(
                "SELECT * FROM character_movements WHERE id = ?", (movement_id,)
            ).fetchone()
        )

    def _plan_route(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        actor: sqlite3.Row,
        destination_longitude: float,
        destination_latitude: float,
        movement_type: str,
        speed_kmh: float,
        selected_vehicle_id: str | None,
    ) -> RoutePlan | None:
        """有审核通过数据集则规划可通行路线；否则返回 None 退回直线移动。"""
        if actor["space_layer"] != "ground":
            return None
        has_dataset = connection.execute(
            """
            SELECT 1 FROM navigation_datasets
            WHERE world_id = ? AND review_status = 'approved' LIMIT 1
            """,
            (world_id,),
        ).fetchone()
        if not has_dataset:
            return None
        vehicle_metadata: dict[str, object] = {"speed_kmh": speed_kmh}
        if selected_vehicle_id:
            vehicle = connection.execute(
                "SELECT * FROM vehicles WHERE id = ?", (selected_vehicle_id,)
            ).fetchone()
            if vehicle is not None and vehicle["metadata_json"]:
                try:
                    vehicle_metadata.update(json.loads(vehicle["metadata_json"]))
                except (TypeError, json.JSONDecodeError):
                    pass
        plan = self.planner.plan(
            connection,
            world_id=world_id,
            origin=(float(actor["longitude"]), float(actor["latitude"])),
            destination=(destination_longitude, destination_latitude),
            movement_type=movement_type,
            vehicle_metadata=vehicle_metadata,
        )
        if not plan.reachable:
            raise RouteUnavailable(plan.unreachable_reason or "该移动无法规划出可通行路线")
        return plan

    def cancel(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        world_time: datetime,
        created_at: datetime | None = None,
        cascade_journey: bool = True,
        interruption_reason: str | None = None,
    ) -> MovementState:
        from world_engine.rides import RideService
        RideService.assert_not_aboard(connection, character_id)
        now = created_at or utc_now()
        row = connection.execute(
            """
            SELECT m.*, c.name, c.location_id, c.current_location_id, c.space_layer
            FROM character_movements m
            JOIN characters c ON c.id = m.character_id
            WHERE m.world_id = ? AND m.character_id = ? AND m.status = 'moving'
            """,
            (world_id, character_id),
        ).fetchone()
        if row is None:
            raise ValueError("人物当前没有进行中的移动")
        connection.execute(
            """
            UPDATE character_movements
            SET status = 'cancelled', updated_at_world = ?, updated_at = ?
            WHERE id = ?
            """,
            (to_iso(world_time), to_iso(now), row["id"]),
        )
        event_id = self._record_log(
            connection,
            world_id=world_id,
            world_time=world_time,
            event_type="action.movement_cancelled",
            actor_id=character_id,
            location_id=row["current_location_id"] or row["location_id"],
            summary=(f"{row['name']}的移动因{interruption_reason}中断，并停留在途中。"
                     if interruption_reason else f"{row['name']}取消了当前移动，并停留在途中。"),
            payload={"movement_id": row["id"], **({"reason": interruption_reason} if interruption_reason else {})},
        )
        from world_engine.action_results import finish_execution

        finish_execution(
            connection, kind="movement", execution_id=row["id"], event_id=event_id,
            world_id=world_id, actor_id=character_id, at=world_time, status="cancelled",
        )
        if cascade_journey:
            from world_engine.group_journeys import GroupJourneyService
            group = GroupJourneyService.active(connection, world_id, character_id)
            if group and connection.execute(
                "SELECT 1 FROM group_journey_leg_members lm JOIN group_journey_legs l ON l.id=lm.leg_id "
                "WHERE l.journey_id=? AND l.status='moving' AND lm.movement_id=? UNION ALL "
                "SELECT 1 FROM group_journey_members WHERE journey_id=? AND meeting_movement_id=? LIMIT 1",
                (group['id'], row['id'], group['id'], row['id']),
            ).fetchone():
                GroupJourneyService.pause(connection, group, world_time, interruption_reason or '本人取消了已绑定的共同移动，原有限授权暂停。')
            from world_engine.journeys import JourneyService
            journey=JourneyService.active(connection,world_id,character_id)
            if journey and connection.execute("SELECT 1 FROM journey_legs WHERE journey_id=? AND status='moving' AND (player_movement_id=? OR npc_movement_id=?)",(journey['id'],row['id'],row['id'])).fetchone():
                JourneyService.pause(connection,journey,world_time,interruption_reason or '一方取消了本程移动，双方停下重新商量')

        # 哥哥，领队主动停下时，只带停这一程真实绑定且仍在同行的成员。
        for linked in connection.execute(
            "SELECT DISTINCT m.character_id FROM companion_movement_links l "
            "JOIN companion_sessions s ON s.id=l.session_id "
            "JOIN character_movements m ON m.id=l.companion_movement_id "
            "WHERE l.leader_movement_id=? AND s.leader_id=? AND s.status='active' "
            "AND m.character_id=s.companion_id AND m.status='moving'",
            (row['id'], character_id),
        ).fetchall():
            self.cancel(connection, world_id=world_id, character_id=linked['character_id'],
                        world_time=world_time, created_at=now, interruption_reason=interruption_reason)
        return WorldRepository.movement_from_row(
            connection.execute(
                "SELECT * FROM character_movements WHERE id = ?", (row["id"],)
            ).fetchone()
        )

    def select_transport(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        vehicle_id: str | None,
    ) -> None:
        from world_engine.rides import RideService
        RideService.assert_not_aboard(connection, character_id)
        if vehicle_id and connection.execute('SELECT 1 FROM vehicle_states WHERE vehicle_id=?', (vehicle_id,)).fetchone():
            raise ValueError('这辆真实载具请经共乘约定登乘，不能远程切换交通工具')
        from world_engine.companions import CompanionService
        from world_engine.player_steering import PlayerSteeringService
        if vehicle_id and connection.execute("SELECT 1 FROM joint_journeys WHERE status='active' AND (proposer_id=? OR recipient_id=?)",(character_id,character_id)).fetchone():
            raise ValueError('请先暂停或结束徒步旅行，再更换交通工具')
        if vehicle_id and CompanionService.active(connection, character_id):
            raise ValueError('请先结束徒步同行，再更换交通工具')
        if PlayerSteeringService.active(connection, character_id):
            raise ValueError("移动过程中不能更换交通工具")
        moving = connection.execute(
            """
            SELECT 1 FROM character_movements
            WHERE character_id = ? AND status = 'moving'
            """,
            (character_id,),
        ).fetchone()
        if moving is not None:
            raise ValueError("移动过程中不能更换交通工具")
        if vehicle_id is None:
            connection.execute(
                """
                UPDATE characters
                SET active_vehicle_id = NULL, movement_type = 'land',
                    movement_speed_kmh = 5, updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (to_iso(utc_now()), character_id, world_id),
            )
            return
        vehicle = ContractService.vehicle_for(connection, world_id, character_id, vehicle_id)
        if vehicle is None:
            raise ValueError("交通工具不可用或不属于当前人物")
        assert_vehicle_fuel_accounted(connection, vehicle)
        from world_engine.group_journeys import GroupJourneyService
        group = GroupJourneyService.active(connection, world_id, character_id)
        if group and group["status"] in {"accepted", "active", "resume_pending"}:
            at = from_iso(connection.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()[0])
            GroupJourneyService.pause(connection, group, at, "本人选择独立载具，原有限徒步跟随已撤回。", force=group["status"] == "resume_pending")
        connection.execute(
            """
            UPDATE characters
            SET active_vehicle_id = ?, movement_type = ?, movement_speed_kmh = ?,
                updated_at = ?
            WHERE id = ? AND world_id = ?
            """,
            (
                vehicle_id,
                vehicle["movement_type"],
                vehicle["speed_kmh"],
                to_iso(utc_now()),
                character_id,
                world_id,
            ),
        )

    def advance(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        previous_time: datetime,
        current_time: datetime,
        created_at: datetime,
    ) -> int:
        from world_engine.companions import CompanionService
        from world_engine.player_steering import PlayerSteeringService

        CompanionService.maintain(connection, world_id, previous_time)
        from world_engine.rides import RideService
        # 哥哥，先在区间起点检查共乘驾驶权，不能走完这一心跳才发现早已无权使用。
        RideService.maintain(connection, world_id, previous_time)
        steering_updated = PlayerSteeringService.advance(connection, world_id, current_time, created_at)
        elapsed_hours = max(0.0, (current_time - previous_time).total_seconds() / 3600)
        if elapsed_hours <= 0:
            return steering_updated
        rows = connection.execute(
            """
            SELECT m.*, c.name, c.location_id, c.current_location_id, c.space_layer,
                   c.longitude AS route_current_longitude, c.latitude AS route_current_latitude
            FROM character_movements m
            JOIN characters c ON c.id = m.character_id
            WHERE m.world_id = ? AND m.status = 'moving'
            ORDER BY m.started_at_world, m.id
            """,
            (world_id,),
        ).fetchall()
        updated = steering_updated
        # 哥哥，同一批人必须在任何一人移动前完成核验；否则坏路线的同伴可能被落下。
        invalid = []
        route_data = {}
        closed_segments = {}
        for row in rows:
            if not self._requires_navigation_route(row):
                continue
            try:
                route = self._validated_navigation_route(row)
                closed_segments[row["id"]] = self._validated_local_routes(connection, row, route)
                route_data[row["id"]] = route
            except RouteDataInvalid:
                invalid.append(row)
        for row in invalid:
            current = connection.execute(
                "SELECT status FROM character_movements WHERE id=? AND world_id=?",
                (row["id"], world_id),
            ).fetchone()
            if current is None or current["status"] != "moving":
                continue
            reason = "已保存的导航路线无效，须在当前位置重新规划"
            connection.execute(
                "UPDATE character_movements SET replan_reason=? WHERE id=? AND world_id=?",
                (reason, row["id"], world_id),
            )
            ride = connection.execute(
                "SELECT * FROM vehicle_rides WHERE world_id=? AND movement_id=? AND status='moving'",
                (world_id, row["id"]),
            ).fetchone()
            if ride:
                RideService._block(connection, ride, previous_time, reason)
            else:
                self._interrupt_invalid_navigation(connection, row, previous_time, created_at, reason)
            updated += 1
        if invalid:
            rows = connection.execute(
                "SELECT m.*, c.name, c.location_id, c.current_location_id, c.space_layer, "
                "c.longitude AS route_current_longitude,c.latitude AS route_current_latitude "
                "FROM character_movements m JOIN characters c ON c.id=m.character_id "
                "WHERE m.world_id=? AND m.status='moving' ORDER BY m.started_at_world,m.id",
                (world_id,),
            ).fetchall()
        navigation_stops = {}
        rows_by_id = {row["id"]: row for row in rows}
        for row in rows:
            route = route_data.get(row["id"])
            if route is None:
                continue
            distance = float(row["distance_travelled_km"])
            cumulative = 0.0
            for index, edge in enumerate(route["edges"]):
                start = cumulative
                cumulative += float(edge["length_km"])
                if index not in closed_segments[row["id"]] or distance >= cumulative - 1e-9:
                    continue
                hours = self._route_hours_between(route, distance, max(distance, start))
                entry_at = previous_time + timedelta(hours=hours)
                exit_at = previous_time + timedelta(hours=self._route_hours_between(route, distance, cumulative))
                closed_at = closed_segments[row["id"]][index]
                stop_at = max(previous_time, entry_at, closed_at)
                if stop_at >= exit_at:
                    continue
                if stop_at <= current_time:
                    reason = "前方已核验通路当前关闭，须停在通路入口或当前位置"
                    for movement_id in self._bound_navigation_movements(connection, row):
                        if movement_id in rows_by_id and (movement_id not in navigation_stops or stop_at < navigation_stops[movement_id][0]):
                            navigation_stops[movement_id] = (stop_at, reason)
                break
        settled = []
        ride_people = set()
        reached_navigation_stops = set()
        for row in rows:
            effective_time = current_time
            ride = connection.execute("SELECT * FROM vehicle_rides WHERE movement_id=? AND status='moving'", (row['id'],)).fetchone()
            ride_deadline = None
            if ride:
                # 哥哥，旧存档可能已经逾期；只能在本轮起点发现并挂起，不能回写过去的事件或位置。
                ride_deadline = max(previous_time, RideService.effective_deadline(connection, ride, previous_time))
                effective_time = min(effective_time, ride_deadline)
            for session in connection.execute(
                "SELECT until_world_time FROM companion_sessions WHERE status='active' AND "
                "(leader_id=? OR companion_id=?)", (row['character_id'], row['character_id'])
            ):
                effective_time = min(effective_time, from_iso(session['until_world_time']))
            if row["vehicle_id"]:
                lease = connection.execute(
                    "SELECT due_world_time FROM contract_fulfillments WHERE asset_id=? AND status='active'",
                    (row["vehicle_id"],),
                ).fetchone()
                if lease:
                    effective_time = min(effective_time, from_iso(lease["due_world_time"]))
            navigation_stop = navigation_stops.get(row["id"])
            if navigation_stop:
                effective_time = min(effective_time, navigation_stop[0])
                if effective_time >= navigation_stop[0]:
                    reached_navigation_stops.add(row["id"])
            allowed_hours = max(0.0, (effective_time - previous_time).total_seconds() / 3600)
            travelled, longitude, latitude, arrived, route_index = self._advance_one(
                row, min(elapsed_hours, allowed_hours)
            )
            # 心跳延迟或浮点累计误差不能让进度已满的行程继续停在 moving。
            if not self._requires_navigation_route(row) and effective_time >= from_iso(row["estimated_arrival_world"]):
                travelled = float(row["total_distance_km"])
                longitude = float(row["destination_longitude"])
                latitude = float(row["destination_latitude"])
                arrived = True
            resolved_location = self.spatial.resolve_location(
                connection,
                world_id=world_id,
                longitude=longitude,
                latitude=latitude,
                space_layer=row["space_layer"],
            )
            current_location_id = resolved_location["id"] if resolved_location is not None else None
            connection.execute(
                """
                UPDATE characters
                SET longitude = ?, latitude = ?,
                    location_id = CASE WHEN ? THEN COALESCE(?, location_id) ELSE location_id END,
                    current_location_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    longitude,
                    latitude,
                    int(arrived),
                    row["destination_location_id"],
                    current_location_id,
                    to_iso(created_at),
                    row["character_id"],
                ),
            )
            connection.execute(
                "UPDATE character_movements SET status=?,distance_travelled_km=?,route_index=?,"
                "updated_at_world=?,updated_at=? WHERE id=?",
                ("arrived" if arrived else "moving", travelled, route_index, to_iso(current_time),
                 to_iso(created_at), row["id"]),
            )
            settled.append((row, longitude, latitude, current_location_id, arrived))
            from world_engine.rides import RideService
            ride_people.update(RideService.project_movement(
                connection, row, longitude, latitude, current_location_id, arrived, effective_time, created_at,
            ))
            if ride and not arrived and ride_deadline is not None and effective_time >= ride_deadline:
                # 哥哥，期限只允许走到截止时刻；未实际抵达时保留乘员与车辆并挂起。
                RideService._block(connection, ride, ride_deadline, "共乘期限已到，本程尚未实际抵达，保留车辆与全部乘员")
        # 哥哥，先把同伴在这一段真实走过的位置一起结算，再暂停共同移动。
        for movement_id, (stop_at, reason) in sorted(navigation_stops.items(), key=lambda item: (item[1][0], item[0])):
            if movement_id not in reached_navigation_stops:
                continue
            row = rows_by_id[movement_id]
            current = connection.execute("SELECT status FROM character_movements WHERE id=?", (movement_id,)).fetchone()
            if current is None or current["status"] != "moving":
                continue
            connection.execute("UPDATE character_movements SET replan_reason=? WHERE id=?", (reason, movement_id))
            ride = connection.execute("SELECT * FROM vehicle_rides WHERE movement_id=? AND status='moving'", (movement_id,)).fetchone()
            if ride:
                RideService._block(connection, ride, stop_at, reason)
            else:
                self._interrupt_invalid_navigation(connection, row, stop_at, created_at, reason)
        from world_engine.encounters import EncounterService

        EncounterService.end_separated(
            connection, world_id, {row["character_id"] for row, *_ in settled} | ride_people, current_time,
        )
        # 哥哥，同一时刻先结算所有人的真实位置，再记录目击；逐人写库不表示同伴曾经分离。
        for row, longitude, latitude, current_location_id, arrived in settled:
            encountered = set(json.loads(row["encountered_character_ids_json"]))
            nearby = set(
                self._nearby_character_ids(
                    connection,
                    world_id=world_id,
                    character_id=row["character_id"],
                    longitude=longitude,
                    latitude=latitude,
                )
            )
            new_encounters = sorted(nearby - encountered)
            encountered.update(new_encounters)
            connection.execute(
                """
                UPDATE character_movements
                SET encountered_character_ids_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(sorted(encountered), ensure_ascii=False),
                    row["id"],
                ),
            )
            for encountered_id in new_encounters:
                encountered_character = connection.execute(
                    "SELECT name FROM characters WHERE id = ?", (encountered_id,)
                ).fetchone()
                if encountered_character:
                    self._record_log(
                        connection,
                        world_id=world_id,
                        world_time=current_time,
                        event_type="action.movement_encounter",
                        actor_id=row["character_id"],
                        target_id=encountered_id,
                        location_id=current_location_id,
                        summary=(f"{row['name']}在移动途中遇见了{encountered_character['name']}。"),
                        payload={"movement_id": row["id"]},
                    )
            if arrived:
                from world_engine.rides import RideService
                RideService.record_arrival(connection, row, current_time)
                event_id = self._record_log(
                    connection,
                    world_id=world_id,
                    world_time=current_time,
                    event_type="action.movement_arrived",
                    actor_id=row["character_id"],
                    location_id=current_location_id,
                    summary=f"{row['name']}抵达了本次移动的目标坐标。",
                    payload={
                        "movement_id": row["id"],
                        "destination_longitude": row["destination_longitude"],
                        "destination_latitude": row["destination_latitude"],
                    },
                )
                from world_engine.action_results import finish_execution

                finish_execution(
                    connection, kind="movement", execution_id=row["id"], event_id=event_id,
                    world_id=world_id, actor_id=row["character_id"], at=current_time,
                    status="completed",
                )
            updated += 1
        CompanionService.maintain(connection, world_id, current_time)
        return updated

    @staticmethod
    def _validated_local_routes(connection, row, route):
        """哥哥，折线的局部段必须仍与实际核准记录逐段相符。"""
        bindings = route.get("local_routes", [])
        edges = route["edges"]
        tagged = [index for index, edge in enumerate(edges) if any(key.startswith("local_route_") for key in edge)]
        if not bindings and not tagged:
            return {}
        try:
            from world_engine.local_routes import LocalRouteService

            if (not isinstance(bindings, list) or len(bindings) != 1 or not tagged or row["movement_type"] != "land"
                    or "space_layer" in row.keys() and row["space_layer"] != "ground"):
                raise ValueError
            binding = bindings[0]
            local_route = LocalRouteService.get(connection, row["world_id"], binding["id"])
            if (local_route["world_id"] != row["world_id"] or local_route["dataset_id"] != row["navigation_dataset_id"]
                    or binding["revision"] != local_route["revision"]
                    or binding["geometry_sha256"] != local_route["geometry_sha256"]
                    or row["movement_type"] not in local_route["allowed_modes"]):
                raise ValueError
            direction = binding["direction"]
            if direction not in {"forward", "reverse"} or direction == "reverse" and not local_route["bidirectional"]:
                raise ValueError
            canonical = local_route["polyline"]
            if (len(tagged) != len(canonical) - 1
                    or tagged != list(range(tagged[0], tagged[0] + len(tagged)))):
                raise ValueError
            for offset, edge_index in enumerate(tagged):
                edge = edges[edge_index]
                segment = offset if direction == "forward" else len(canonical) - 2 - offset
                expected = canonical[segment:segment + 2]
                if direction == "reverse":
                    expected = list(reversed(expected))
                if (edge.get("local_route_id") != local_route["id"]
                        or edge.get("local_route_revision") != local_route["revision"]
                        or edge.get("local_route_geometry_sha256") != local_route["geometry_sha256"]
                        or edge.get("local_route_direction") != direction
                        or edge.get("local_route_segment_index") != segment
                        or float(edge["speed_kmh"]) > float(local_route["speed_limit_kmh"]) + 1e-9
                        or any(great_circle_distance_km(*route["polyline"][edge_index + point], *expected[point]) > 1e-6
                               for point in (0, 1))):
                    raise ValueError
            if local_route["is_open"]:
                return {}
            closed_at = from_iso(local_route["state_changed_at"])
            return {index: closed_at for index in tagged}
        except (TypeError, ValueError, LookupError, OverflowError) as exc:
            raise RouteDataInvalid("局部通路的已保存绑定无效") from exc

    @staticmethod
    def _route_hours_between(route, start, finish):
        hours = 0.0
        cumulative = 0.0
        for edge in route["edges"]:
            end = cumulative + float(edge["length_km"])
            distance = max(0.0, min(finish, end) - max(start, cumulative))
            hours += distance / float(edge["speed_kmh"])
            cumulative = end
            if cumulative >= finish:
                break
        return hours

    @staticmethod
    def _bound_navigation_movements(connection, row):
        """哥哥，关闭通路会让本程真实绑定的同伴在同一时刻停下。"""
        wid, movement_id = row["world_id"], row["id"]
        result = {movement_id}
        for link in connection.execute(
            "SELECT l.leader_movement_id,l.companion_movement_id FROM companion_movement_links l "
            "JOIN companion_sessions s ON s.id=l.session_id WHERE s.world_id=? AND s.status='active'",
            (wid,),
        ):
            if movement_id in {link["leader_movement_id"], link["companion_movement_id"]}:
                leader_id = link["leader_movement_id"]
                result.add(leader_id)
                result.update(member[0] for member in connection.execute(
                    "SELECT l.companion_movement_id FROM companion_movement_links l "
                    "JOIN companion_sessions s ON s.id=l.session_id "
                    "WHERE s.world_id=? AND s.status='active' AND l.leader_movement_id=?", (wid, leader_id)))
        for leg in connection.execute(
            "SELECT l.player_movement_id,l.npc_movement_id FROM journey_legs l "
            "JOIN joint_journeys j ON j.id=l.journey_id WHERE j.world_id=? AND j.status='active' AND l.status='moving' "
            "AND (l.player_movement_id=? OR l.npc_movement_id=?)", (wid, movement_id, movement_id),
        ):
            result.update((leg["player_movement_id"], leg["npc_movement_id"]))
        groups = connection.execute(
            "SELECT DISTINCT g.id FROM group_journeys g LEFT JOIN group_journey_members gm ON gm.journey_id=g.id "
            "LEFT JOIN group_journey_legs l ON l.journey_id=g.id AND l.status='moving' "
            "LEFT JOIN group_journey_leg_members lm ON lm.leg_id=l.id "
            "WHERE g.world_id=? AND g.status IN ('accepted','active','paused','resume_pending') "
            "AND ((gm.left_at IS NULL AND gm.meeting_movement_id=?) OR lm.movement_id=?)",
            (wid, movement_id, movement_id),
        ).fetchall()
        for group in groups:
            result.update(member[0] for member in connection.execute(
                "SELECT meeting_movement_id FROM group_journey_members WHERE journey_id=? AND left_at IS NULL "
                "UNION SELECT lm.movement_id FROM group_journey_legs l "
                "JOIN group_journey_leg_members lm ON lm.leg_id=l.id WHERE l.journey_id=? AND l.status='moving'",
                (group["id"], group["id"])))
        return result

    def _interrupt_invalid_navigation(self, connection, row, at, created_at, reason):
        """哥哥，先停下关联的真实移动，再交由既有旅行状态机暂停。"""
        from world_engine.group_journeys import GroupJourneyService
        from world_engine.journeys import JourneyService

        wid, cid, movement_id = row["world_id"], row["character_id"], row["id"]
        group = GroupJourneyService.active(connection, wid, cid)
        if group and connection.execute(
            "SELECT 1 FROM group_journey_leg_members lm JOIN group_journey_legs l ON l.id=lm.leg_id "
            "WHERE l.journey_id=? AND l.status='moving' AND lm.movement_id=? UNION ALL "
            "SELECT 1 FROM group_journey_members WHERE journey_id=? AND meeting_movement_id=? LIMIT 1",
            (group["id"], movement_id, group["id"], movement_id),
        ).fetchone():
            members = connection.execute(
                "SELECT m.character_id FROM group_journey_members gm JOIN character_movements m "
                "ON m.id=gm.meeting_movement_id AND m.character_id=gm.character_id "
                "WHERE gm.journey_id=? AND m.world_id=? AND m.status='moving' UNION "
                "SELECT m.character_id FROM group_journey_legs l JOIN group_journey_leg_members lm ON lm.leg_id=l.id "
                "JOIN character_movements m ON m.id=lm.movement_id AND m.character_id=lm.character_id "
                "WHERE l.journey_id=? AND l.status='moving' AND m.world_id=? AND m.status='moving'",
                (group["id"], wid, group["id"], wid),
            ).fetchall()
            for member in members:
                self.cancel(connection, world_id=wid, character_id=member["character_id"], world_time=at,
                            created_at=created_at, cascade_journey=False, interruption_reason=reason)
            GroupJourneyService.pause(connection, group, at, reason)
            return
        journey = JourneyService.active(connection, wid, cid)
        if journey:
            leg = connection.execute(
                "SELECT * FROM journey_legs WHERE journey_id=? AND status='moving' "
                "AND (player_movement_id=? OR npc_movement_id=?) LIMIT 1",
                (journey["id"], movement_id, movement_id),
            ).fetchone()
            if leg:
                for key in ("player_movement_id", "npc_movement_id"):
                    member = connection.execute(
                        "SELECT character_id FROM character_movements WHERE id=? AND world_id=? AND status='moving'",
                        (leg[key], wid),
                    ).fetchone()
                    if member:
                        self.cancel(connection, world_id=wid, character_id=member["character_id"], world_time=at,
                                    created_at=created_at, cascade_journey=False, interruption_reason=reason)
                JourneyService.pause(connection, journey, at, reason)
                return
        # 哥哥，短时同行的任一绑定路线损坏，都先停止这一程的领队和同伴。
        leader = connection.execute(
            "SELECT m.character_id FROM companion_movement_links l "
            "JOIN companion_sessions s ON s.id=l.session_id "
            "JOIN character_movements m ON m.id=l.leader_movement_id "
            "WHERE s.world_id=? AND s.status='active' AND m.status='moving' "
            "AND (l.leader_movement_id=? OR l.companion_movement_id=?) LIMIT 1",
            (wid, movement_id, movement_id),
        ).fetchone()
        self.cancel(connection, world_id=wid, character_id=leader["character_id"] if leader else cid,
                    world_time=at, created_at=created_at, interruption_reason=reason)

    @staticmethod
    def _nearest_location(
        connection: sqlite3.Connection,
        world_id: str,
        longitude: float,
        latitude: float,
        space_layer: str = "ground",
    ) -> sqlite3.Row | None:
        locations = connection.execute(
            "SELECT id, longitude, latitude FROM locations WHERE world_id = ? AND is_active = 1 AND space_layer = ?",
            (world_id, space_layer),
        ).fetchall()
        if not locations:
            return None
        nearest = min(
            locations,
            key=lambda item: great_circle_distance_km(
                longitude, latitude, item["longitude"], item["latitude"]
            ),
        )
        distance = great_circle_distance_km(
            longitude,
            latitude,
            nearest["longitude"],
            nearest["latitude"],
        )
        return nearest if distance <= 20.0 else None

    def _advance_one(
        self,
        row: sqlite3.Row,
        elapsed_hours: float,
    ) -> tuple[float, float, float, bool, int]:
        """推进单个移动；有路线则沿折线逐段推进，否则退回直线插值。"""
        if self._requires_navigation_route(row):
            return self._walk_route(row, self._validated_navigation_route(row), elapsed_hours)
        raw = row["route_json"]
        if not isinstance(raw, str) or not raw:
            return self._advance_legacy(row, elapsed_hours)
        try:
            route = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return self._advance_legacy(row, elapsed_hours)
        if not isinstance(route, dict) or not route.get("polyline") or not route.get("edges"):
            return self._advance_legacy(row, elapsed_hours)
        return self._walk_route(row, route, elapsed_hours)

    @staticmethod
    def _requires_navigation_route(row):
        """哥哥，任一尚存的导航痕迹都不能被误当成无路线的旧移动。"""
        if row["navigation_dataset_id"] is not None:
            return True
        try:
            distance = float(row["route_distance_km"] or 0)
            if not math.isfinite(distance) or distance != 0:
                return True
        except (TypeError, ValueError, OverflowError):
            return True
        try:
            value = json.loads(row["route_json"])
        except (TypeError, ValueError):
            return False
        if not isinstance(value, dict):
            return False
        if value.get("dataset_id") or "local_routes" in value:
            return True
        edges = value.get("edges")
        return isinstance(edges, list) and any(
            isinstance(edge, dict) and any(key.startswith("local_route_") for key in edge)
            for edge in edges
        )

    @staticmethod
    def _validated_navigation_route(row):
        """哥哥，已绑定地形数据的路线必须可逐段核验，不能作为旧版直线处理。"""
        try:
            route = json.loads(row["route_json"])
            if (not row["navigation_dataset_id"] or not isinstance(route, dict) or route.get("reachable") is not True
                    or route.get("dataset_id") != row["navigation_dataset_id"]
                    or route.get("movement_type") != row["movement_type"]):
                raise ValueError
            points, edges = route["polyline"], route["edges"]
            if not isinstance(points, list) or not isinstance(edges, list) or not edges or len(points) != len(edges) + 1:
                raise ValueError
            coordinates = []
            for point in points:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError
                lon, lat = float(point[0]), float(point[1])
                if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                    raise ValueError
                coordinates.append((lon, lat))
            if (great_circle_distance_km(*coordinates[0], row["origin_longitude"], row["origin_latitude"]) > 0.000001
                    or great_circle_distance_km(*coordinates[-1], row["destination_longitude"], row["destination_latitude"]) > 0.000001):
                raise ValueError
            total = 0.0
            lengths = []
            cumulative = []
            for index, edge in enumerate(edges):
                length, speed = float(edge["length_km"]), float(edge["speed_kmh"])
                actual = great_circle_distance_km(*coordinates[index], *coordinates[index + 1])
                if (not math.isfinite(length) or not math.isfinite(speed) or length < 0 or speed <= 0
                        or abs(length - actual) > 0.00011):
                    raise ValueError
                total += length
                lengths.append(length)
                cumulative.append(total)
            distance = float(row["distance_travelled_km"])
            raw_index = float(row["route_index"])
            index = int(raw_index)
            recorded_total = float(row["total_distance_km"])
            if (total <= 0 or not math.isfinite(distance) or distance < 0
                    or not math.isfinite(recorded_total) or not math.isfinite(raw_index) or raw_index != index
                    or distance > total + 0.001 + len(edges) * 0.00005
                    or not 0 <= index <= len(edges)
                    or abs(recorded_total - total) > 0.001 + len(edges) * 0.00005):
                raise ValueError
            lower = cumulative[index - 1] if index else 0.0
            upper = cumulative[index] if index < len(edges) else total
            if not lower - 1e-8 <= distance <= upper + 1e-8:
                raise ValueError
            if "route_current_longitude" in row.keys():
                actual = (float(row["route_current_longitude"]), float(row["route_current_latitude"]))
                expected = MovementService._interpolate_along(coordinates, cumulative, lengths, distance, total)
                if (not all(math.isfinite(value) for value in actual)
                        or great_circle_distance_km(*actual, *expected) > 0.001):
                    raise ValueError
            return route
        except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:
            raise RouteDataInvalid("已保存的导航路线无效") from exc

    def _walk_route(
        self,
        row: sqlite3.Row,
        route: dict[str, object],
        elapsed_hours: float,
    ) -> tuple[float, float, float, bool, int]:
        polyline = route.get("polyline") or []
        edges = route.get("edges") or []
        if len(polyline) < 2 or not edges:
            return self._advance_legacy(row, elapsed_hours)
        points = [(float(p[0]), float(p[1])) for p in polyline]
        edge_lengths = [float(item["length_km"]) for item in edges]
        edge_speeds = [float(item["speed_kmh"]) for item in edges]
        cumulative: list[float] = []
        running = 0.0
        for length in edge_lengths:
            running += length
            cumulative.append(running)
        total = cumulative[-1]
        if total <= 0:
            return self._advance_legacy(row, elapsed_hours)

        current_dist = min(float(row["distance_travelled_km"]), total)
        index = min(int(row["route_index"]), len(edges))
        hours_left = max(0.0, elapsed_hours)
        while hours_left > 1e-9 and index < len(edges):
            seg_end = cumulative[index]
            remaining = seg_end - current_dist
            speed = edge_speeds[index]
            if speed <= 0:
                break
            time_to_finish = remaining / speed
            if hours_left >= time_to_finish:
                hours_left -= time_to_finish
                current_dist = seg_end
                index += 1
            else:
                current_dist += hours_left * speed
                hours_left = 0.0
        travelled = min(current_dist, total)
        longitude, latitude = self._interpolate_along(
            points, cumulative, edge_lengths, travelled, total
        )
        arrived = travelled >= total - 1e-9
        new_index = len(edges) if arrived else index
        return travelled, longitude, latitude, arrived, new_index

    @staticmethod
    def _interpolate_along(
        points: list[tuple[float, float]],
        cumulative: list[float],
        edge_lengths: list[float],
        distance: float,
        total: float,
    ) -> tuple[float, float]:
        if distance <= 0 or not points:
            return points[0] if points else (0.0, 0.0)
        if distance >= total - 1e-9:
            return points[-1]
        target = min(distance, total)
        for index, edge_length in enumerate(edge_lengths):
            seg_start = cumulative[index] - edge_length
            if target <= cumulative[index] + 1e-9:
                ratio = 0.0 if edge_length <= 0 else (target - seg_start) / edge_length
                longitude, latitude = MovementService._interpolate_point(
                    points[index], points[index + 1], ratio
                )
                return longitude, latitude
        return points[-1]

    @staticmethod
    def _interpolate_point(
        first: tuple[float, float], second: tuple[float, float], ratio: float
    ) -> tuple[float, float]:
        """沿两点线性插值，经度跨 ±180 时按最短方向绕行，避免穿越到对侧半球。"""
        longitude_delta = second[0] - first[0]
        if longitude_delta > 180:
            longitude_delta -= 360
        elif longitude_delta < -180:
            longitude_delta += 360
        longitude = first[0] + longitude_delta * ratio
        if longitude > 180:
            longitude -= 360
        elif longitude < -180:
            longitude += 360
        latitude = first[1] + (second[1] - first[1]) * ratio
        return longitude, latitude

    def _advance_legacy(
        self, row: sqlite3.Row, elapsed_hours: float
    ) -> tuple[float, float, float, bool, int]:
        travelled = min(
            float(row["total_distance_km"]),
            float(row["distance_travelled_km"]) + float(row["speed_kmh"]) * elapsed_hours,
        )
        total = float(row["total_distance_km"])
        progress = 1.0 if total <= 0 else travelled / total
        longitude, latitude = interpolate_coordinate(
            row["origin_longitude"],
            row["origin_latitude"],
            row["destination_longitude"],
            row["destination_latitude"],
            progress,
        )
        return travelled, longitude, latitude, progress >= 1.0 - 1e-9, int(row["route_index"])

    @staticmethod
    def _nearby_character_ids(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        longitude: float,
        latitude: float,
    ) -> list[str]:
        south, north, ranges = nearby_coordinate_bounds(longitude, latitude, ENCOUNTER_RADIUS_KM)
        longitude_filter = " OR ".join("longitude BETWEEN ? AND ?" for _ in ranges)
        rows = connection.execute(
            """
            SELECT id, longitude, latitude
            FROM characters
            WHERE world_id = ? AND id <> ? AND health > 0
            AND space_layer=(SELECT space_layer FROM characters WHERE id=?)
            AND current_room_id IS (SELECT current_room_id FROM characters WHERE id=?)
            AND latitude BETWEEN ? AND ? AND (
            """ + longitude_filter + ")",
            (world_id, character_id, character_id, character_id, south, north,
             *(value for pair in ranges for value in pair)),
        ).fetchall()
        return [
            row["id"]
            for row in rows
            if great_circle_distance_km(longitude, latitude, row["longitude"], row["latitude"])
            <= ENCOUNTER_RADIUS_KM
        ]

    @staticmethod
    def _record_log(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        world_time: datetime,
        event_type: str,
        actor_id: str | None,
        location_id: str | None,
        summary: str,
        payload: dict[str, object],
        target_id: str | None = None,
    ) -> str:
        from world_engine.event_log import record_event

        # 哥哥，移动也登记本人见闻，出发、取消和抵达才能出现在旅程日志中。
        return record_event(
            connection, world_id=world_id, tick_id=str(uuid4()), occurred_at=world_time,
            event_type=event_type, actor_id=actor_id, target_id=target_id,
            location_id=location_id, summary=summary, payload=payload,
        )
