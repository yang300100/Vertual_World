"""方向输入直接推进真实坐标；哥哥松键后不再留下目标行程。"""

from __future__ import annotations

import json
import math
from datetime import timedelta

from world_engine.contracts import ContractService
from world_engine.life import LifeActivityService
from world_engine.navigation import load_terrain
from world_engine.repository import from_iso, to_iso
from world_engine.routing import RoutePlanner
from world_engine.spatial import SpatialContextService

INPUT_LEASE_SECONDS = 2.0
STEP_KM = 0.025


def _terrain(asset_root):
    return load_terrain(asset_root)


class PlayerSteeringService:
    @staticmethod
    def active(connection, character_id):
        return connection.execute(
            "SELECT * FROM player_steering WHERE character_id=? AND (dx!=0 OR dy!=0)",
            (character_id,),
        ).fetchone()

    @staticmethod
    def capabilities(connection, actor):
        from world_engine.rides import RideService
        RideService.assert_not_aboard(connection, actor['id'])
        LifeActivityService.assert_available(connection, actor["id"])
        if actor["health"] <= 0:
            raise ValueError("当前身体状态无法移动")
        if actor["current_room_id"] or actor["current_fixture_id"]:
            raise ValueError("请先起身并离开房间，再开始室外移动")
        speed, kind = float(actor["movement_speed_kmh"]), actor["movement_type"]
        metadata = {}
        if actor["active_vehicle_id"]:
            if connection.execute('SELECT 1 FROM vehicle_states WHERE vehicle_id=?', (actor['active_vehicle_id'],)).fetchone():
                raise ValueError('真实载具请从原共乘路线驾驶，不能另开方向路线')
            vehicle = ContractService.vehicle_for(connection, actor["world_id"], actor["id"],
                                                  actor["active_vehicle_id"])
            if vehicle is None:
                raise ValueError("所选交通工具不可用或不属于当前人物")
            speed, kind = float(vehicle["speed_kmh"]), vehicle["movement_type"]
            metadata = json.loads(vehicle["metadata_json"] or "{}")
        if speed <= 0 or not math.isfinite(speed):
            raise ValueError("当前人物无法移动")
        from world_engine.companions import CompanionService

        speed = CompanionService.speed_cap(connection, actor, speed)
        return speed, kind, metadata

    @staticmethod
    def record(connection, row, at, event_type, reason):
        from world_engine.actions import ActionService
        from world_engine.domain import ActionProposal, ActionType
        from world_engine.movement import MovementService

        actor = connection.execute("SELECT * FROM characters WHERE id=?",
                                   (row["character_id"],)).fetchone()
        observed = json.loads(json.dumps(ActionService._observed_state(connection, actor["id"])))
        before = json.loads(row["started_state_json"] or "{}") or observed
        event_id = MovementService._record_log(
            connection, world_id=row["world_id"], world_time=at, event_type=event_type,
            actor_id=actor["id"], location_id=actor["current_location_id"],
            summary=f"{actor['name']}{reason}",
            payload={"dx": row["dx"], "dy": row["dy"], "longitude": actor["longitude"],
                     "latitude": actor["latitude"], "reason": reason},
        )
        if event_type != "action.steering_changed":
            ActionService._record_effect(
                connection, row["world_id"], event_id,
                ActionProposal(actor_id=actor["id"], action=ActionType.TRAVEL, reason=reason),
                at, before, observed, None, None, event_type,
            )

    @classmethod
    def stop(cls, connection, world_id, at, reason="松开方向键，停止移动。"):
        row = connection.execute("SELECT * FROM player_steering WHERE world_id=?",
                                 (world_id,)).fetchone()
        if row and (row["dx"] or row["dy"]):
            connection.execute("UPDATE player_steering SET dx=0,dy=0 WHERE world_id=?",
                               (world_id,))
            cls.record(connection, row, at, "action.steering_stopped", reason)

    @classmethod
    def set_direction(cls, connection, actor, direction, sequence, at, now):
        connection.execute("SAVEPOINT group_direction_input")
        try:
            cls._set_direction(connection, actor, direction, sequence, at, now)
        except Exception:
            connection.execute("ROLLBACK TO group_direction_input")
            raise
        finally:
            connection.execute("RELEASE group_direction_input")

    @classmethod
    def _set_direction(cls, connection, actor, direction, sequence, at, now):
        from world_engine.actions import ActionService

        previous = connection.execute("SELECT * FROM player_steering WHERE world_id=?",
                                      (actor["world_id"],)).fetchone()
        active = previous and (previous["dx"] or previous["dy"])
        if not any(direction):
            cls.stop(connection, actor["world_id"], at)
        else:
            from world_engine.journeys import JourneyService
            if JourneyService.active(connection,actor["world_id"],actor["id"]):
                raise ValueError("请先暂停或退出共同旅行，再使用方向移动")
            cls.capabilities(connection, actor)
            if actor["active_vehicle_id"]:
                from world_engine.movement import assert_vehicle_fuel_accounted
                vehicle = ContractService.vehicle_for(connection,actor["world_id"],actor["id"],actor["active_vehicle_id"])
                # 哥哥，仅限制新方向输入；既有方向推进仍遵循原租期与超时规则。
                assert_vehicle_fuel_accounted(connection, vehicle)
            from world_engine.group_journeys import GroupJourneyService
            group = GroupJourneyService.active(connection, actor["world_id"], actor["id"])
            if group and group["status"] in {"accepted", "active", "resume_pending"}:
                GroupJourneyService.pause(connection, group, at, "本人选择独立方向移动，原有限跟随已撤回。", actor=actor, force=group["status"] == "resume_pending")
        scale = connection.execute("SELECT time_scale FROM world_clock WHERE world_id=?",
                                   (actor["world_id"],)).fetchone()[0]
        before = (previous["started_state_json"] if active else
                  json.dumps(ActionService._observed_state(connection, actor["id"]),
                             ensure_ascii=False))
        connection.execute(
            "INSERT INTO player_steering(world_id,character_id,sequence,dx,dy,updated_at,"
            "advanced_world,expires_world,expires_real,started_state_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(world_id) DO UPDATE SET character_id=excluded.character_id,"
            "sequence=excluded.sequence,dx=excluded.dx,dy=excluded.dy,updated_at=excluded.updated_at,"
            "advanced_world=excluded.advanced_world,expires_world=excluded.expires_world,"
            "expires_real=excluded.expires_real,started_state_json=excluded.started_state_json",
            (actor["world_id"], actor["id"], sequence, *direction, to_iso(now), to_iso(at),
             to_iso(at + timedelta(seconds=INPUT_LEASE_SECONDS * scale)),
             to_iso(now + timedelta(seconds=INPUT_LEASE_SECONDS)), before),
        )
        row = connection.execute("SELECT * FROM player_steering WHERE world_id=?",
                                 (actor["world_id"],)).fetchone()
        if any(direction) and (not active or (previous["dx"], previous["dy"]) != direction):
            cls.record(connection, row, at,
                       "action.steering_changed" if active else "action.steering_started",
                       "改变了移动方向。" if active else "按方向开始移动。")

    @classmethod
    def advance(cls, connection, world_id, current_time, now):
        row = connection.execute(
            "SELECT * FROM player_steering WHERE world_id=? AND (dx!=0 OR dy!=0)",
            (world_id,),
        ).fetchone()
        if not row:
            return 0
        actor = connection.execute("SELECT * FROM characters WHERE id=?",
                                   (row["character_id"],)).fetchone()
        from world_engine.companions import CompanionService

        members = CompanionService.members(connection, actor)
        effective = current_time
        for session in CompanionService.active(connection, actor['id']):
            effective = min(effective, from_iso(session['until_world_time']))
        speed = min((float(n['movement_speed_kmh']) for n in members), default=float('inf'))
        moved = cls._advance_row(connection, row, actor, effective, now, speed)
        for npc in members:
            member_row = {**dict(row), 'character_id': npc['id']}
            moved += cls._advance_row(connection, member_row, npc, effective, now, speed)
        return moved

    @classmethod
    def _advance_row(cls, connection, row, actor, current_time, now, speed_cap):
        """哥哥，每人从自己的真实坐标走，地形仍逐段检查。"""
        world_id = row['world_id']
        try:
            speed, kind, metadata = cls.capabilities(connection, actor)
        except ValueError as exc:
            cls.stop(connection, world_id, current_time, f"停止移动：{exc}。")
            return 0
        speed = min(speed, speed_cap)
        effective = min(current_time, from_iso(row["expires_world"]))
        lease_due = None
        if actor["active_vehicle_id"]:
            lease = connection.execute(
                "SELECT due_world_time FROM contract_fulfillments WHERE asset_id=? AND status='active'",
                (actor["active_vehicle_id"],),
            ).fetchone()
            if lease:
                lease_due = from_iso(lease["due_world_time"])
                effective = min(effective, lease_due)
        seconds = max(0, (effective - from_iso(row["advanced_world"])).total_seconds())
        longitude, latitude = actor["longitude"], actor["latitude"]
        planner = RoutePlanner()
        dataset = planner._approved_dataset(connection, world_id)
        terrain = _terrain(planner._resolve_asset_root(dataset["asset_root"])) if dataset else None
        if actor["space_layer"] != "ground":
            terrain = None
        reason = None
        # 每段最多 25 米，沿输入方向检查地形，哥哥遇到障碍就停下。
        for _ in range(10000):
            if seconds <= 1e-8:
                break
            segment_speed = speed
            if terrain:
                if not (terrain.bounds["min_longitude"] <= longitude <= terrain.bounds["max_longitude"]
                        and terrain.bounds["min_latitude"] <= latitude <= terrain.bounds["max_latitude"]):
                    reason = "超出已审核地形范围"
                    break
                cell = terrain.nearest_cell(longitude, latitude)
                if cell is None:
                    reason = "超出已审核地形范围"
                    break
                passable, reason = planner._passable(terrain, cell, kind, metadata)
                if not passable:
                    break
                segment_speed = terrain.cell_speed_kmh(cell, speed, kind)
            distance = min(STEP_KM, segment_speed * seconds / 3600)
            next_lat = latitude + row["dy"] * math.degrees(distance / 6371.0088)
            next_lon = longitude + row["dx"] * math.degrees(
                distance / (6371.0088 * max(.001, math.cos(math.radians(latitude)))))
            if not (-180 <= next_lon <= 180 and -89.99 <= next_lat <= 89.99):
                reason = "已到达地图边界"
                break
            if actor["space_layer"] != "ground":
                from world_engine.wonders import WonderService
                if not WonderService.contains(connection,world_id,actor["space_layer"],next_lon,next_lat):
                    reason = "已到本层城市边界，请经升降通道离城"
                    break
            if terrain:
                if not (terrain.bounds["min_longitude"] <= next_lon <= terrain.bounds["max_longitude"]
                        and terrain.bounds["min_latitude"] <= next_lat <= terrain.bounds["max_latitude"]):
                    reason = "超出已审核地形范围"
                    break
                cell = terrain.nearest_cell(next_lon, next_lat)
                if cell is None:
                    reason = "超出已审核地形范围"
                    break
                passable, reason = planner._passable(terrain, cell, kind, metadata)
                if not passable:
                    break
            longitude, latitude = next_lon, next_lat
            seconds -= distance / max(.001, segment_speed) * 3600
        else:
            reason = "本次方向输入的移动距离过长，请重新按键"
        moved = (longitude, latitude) != (actor["longitude"], actor["latitude"])
        if moved:
            location = SpatialContextService().resolve_location(
                connection, world_id=world_id, longitude=longitude, latitude=latitude,
                space_layer=actor["space_layer"])
            connection.execute(
                "UPDATE characters SET longitude=?,latitude=?,current_location_id=?,updated_at=? WHERE id=?",
                (longitude, latitude, location["id"] if location else None, to_iso(now), actor["id"]),
            )
        connection.execute("UPDATE player_steering SET advanced_world=? WHERE world_id=?",
                           (to_iso(current_time), world_id))
        if lease_due and current_time >= lease_due:
            reason = "交通工具租期已经结束"
        if reason or current_time >= from_iso(row["expires_world"]) or now >= from_iso(row["expires_real"]):
            cls.stop(connection, world_id, effective,
                     f"停止移动：{reason}。" if reason else "方向输入超时，停止移动。")
        return int(moved)
