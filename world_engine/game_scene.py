"""地图当前画面需要的轻量位置快照。"""

from __future__ import annotations

import math
import sqlite3

from world_engine.geo import great_circle_distance_km


class GameSceneService:
    @staticmethod
    def snapshot(connection: sqlite3.Connection, world_id: str) -> dict[str, object]:
        world = connection.execute(
            "SELECT w.current_time,c.time_scale FROM worlds w "
            "JOIN world_clock c ON c.world_id=w.id WHERE w.id=?", (world_id,),
        ).fetchone()
        if world is None:
            raise LookupError(world_id)
        people = connection.execute(
            "SELECT id,name,identity,longitude,latitude,current_location_id,is_player,is_pov,space_layer,altitude_m "
            "FROM characters WHERE world_id=? AND health>0 ORDER BY id", (world_id,),
        ).fetchall()
        player = next((row for row in people if row["is_player"] and row["is_pov"]), None)
        if player is None:
            return {"world_time": world["current_time"], "time_scale": world["time_scale"],
                    "map": None, "player": None, "npcs": []}
        locations = connection.execute(
            "SELECT id,name,kind,longitude,latitude,area_radius_km,area_priority,parent_location_id,space_layer,altitude_m "
            "FROM locations WHERE world_id=? AND is_active=1", (world_id,),
        ).fetchall()
        inside = [
            (row, great_circle_distance_km(player["longitude"], player["latitude"],
                                           row["longitude"], row["latitude"]))
            for row in locations
            if row["space_layer"] == player["space_layer"] and great_circle_distance_km(player["longitude"], player["latitude"],
                                        row["longitude"], row["latitude"])
            <= row["area_radius_km"]
        ]
        inside = [item for item in inside if item[0]["kind"] in
                  {"city", "town", "village", "settlement"}]
        inside.sort(key=lambda item: (-item[0]["area_priority"], item[0]["area_radius_km"],
                                      item[1]))
        maps = connection.execute(
            "SELECT * FROM world_maps WHERE world_id=? AND review_status='approved'",
            (world_id,),
        ).fetchall()
        by_location = {row["location_id"]: row for row in maps
                       if row["location_id"] and row["map_role"] == "detail"}
        by_id = {row["id"]: row for row in locations}
        selected = None
        for location, _distance in inside:
            current = location
            while current:
                candidate = by_location.get(current["id"])
                if candidate and (
                    candidate["min_longitude"] <= player["longitude"] <= candidate["max_longitude"]
                    and candidate["min_latitude"] <= player["latitude"] <= candidate["max_latitude"]
                ):
                    selected = candidate
                    break
                current = by_id.get(current["parent_location_id"])
            if selected:
                break
        if selected is not None:
            scene_map = dict(selected)
        elif inside:
            # 城镇边缘可能超出详图，局部视图始终包住当前人物。
            location = inside[0][0]
            radius_km = max(1, location["area_radius_km"])
            latitude_radius = radius_km / 100
            longitude_radius = radius_km / max(10, 100 * abs(math.cos(math.radians(location["latitude"]))))
            scene_map = {
                "id": f"local:{location['id']}", "name": location["name"],
                "map_role": "local", "asset_path": None, "location_id": location["id"],
                "min_longitude": max(-180, location["longitude"] - longitude_radius),
                "max_longitude": min(180, location["longitude"] + longitude_radius),
                "min_latitude": max(-90, location["latitude"] - latitude_radius),
                "max_latitude": min(90, location["latitude"] + latitude_radius),
            }
        else:
            world_map = next((row for row in maps if row["map_role"] == "world"
                              and row["zoom_level"] == 0), None)
            scene_map = dict(world_map) if world_map else {
                "id": "world", "name": "世界地图", "map_role": "world",
                "asset_path": None, "location_id": None,
                "min_longitude": -180, "max_longitude": 180,
                "min_latitude": -90, "max_latitude": 90,
            }
        npcs = [dict(row) for row in people if not row["is_player"]
                and row["space_layer"] == player["space_layer"]
                and scene_map["min_longitude"] <= row["longitude"] <= scene_map["max_longitude"]
                and scene_map["min_latitude"] <= row["latitude"] <= scene_map["max_latitude"]]
        scene_map["space_layer"] = player["space_layer"]
        scene_map["altitude_m"] = player["altitude_m"]
        steering = connection.execute("SELECT * FROM player_steering WHERE world_id=?",
                                      (world_id,)).fetchone()
        from world_engine.wonders import WonderService
        wonders = [w for w in WonderService.view(connection,world_id) if w["status"] == "active"]
        return {"world_time": world["current_time"], "time_scale": world["time_scale"],
                "space_layer":player["space_layer"], "wonders":wonders,
                "map": scene_map, "player": dict(player), "npcs": npcs,
                "steering": {"active": bool(steering and (steering["dx"] or steering["dy"])),
                             "dx": steering["dx"] if steering else 0,
                             "dy": steering["dy"] if steering else 0,
                             "sequence": steering["sequence"] if steering else 0}}
