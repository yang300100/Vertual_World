"""为编年者展示已有车辆、物品及事件，规格仍须另行确认。"""

import json

from fastapi import APIRouter

from world_engine.api_deps import require_world_time
from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso
from world_engine.vehicle_fuel_inventory import VehicleFuelInventoryService


def build_vehicle_fuel_profile_options_router(database):
    router = APIRouter(prefix="/api/worlds/{world_id}/vehicle-fuel", tags=["载具燃料"])

    @router.get("/profile-options")
    def profile_options(world_id: str):
        with database.read() as c:
            at = require_world_time(c, world_id)
            vehicles = c.execute(
                "SELECT v.id,v.name,s.* FROM vehicles v JOIN vehicle_states s ON s.vehicle_id=v.id AND s.world_id=v.world_id "
                "WHERE v.world_id=? AND v.is_available=1 AND s.status='parked' AND s.current_location_id IS NOT NULL "
                "AND NOT EXISTS(SELECT 1 FROM vehicle_fuel_profiles p WHERE p.vehicle_id=v.id) ORDER BY v.name,v.id",
                (world_id,),
            ).fetchall()
            available = []
            # 哥哥，全世界只读取一次有界的近期事件，避免每辆车扫描整个日志。
            recent = c.execute("SELECT rowid AS event_order,* FROM world_events WHERE world_id=? AND occurred_at<=? ORDER BY occurred_at DESC,created_at DESC LIMIT 1000", (world_id, to_iso(at))).fetchall() if vehicles else []
            for vehicle in vehicles:
                vid = vehicle["id"]
                if (c.execute("SELECT 1 FROM character_movements WHERE vehicle_id=? AND status IN ('moving','suspended')", (vid,)).fetchone()
                    or c.execute("SELECT 1 FROM characters WHERE active_vehicle_id=?", (vid,)).fetchone()
                    or c.execute("SELECT 1 FROM vehicle_rides WHERE vehicle_id=? AND status IN ('pending','accepted','boarding','moving','arrived','blocked')", (vid,)).fetchone()
                    or c.execute("SELECT 1 FROM vehicle_rides r JOIN vehicle_ride_members m ON m.ride_id=r.id WHERE r.vehicle_id=? AND m.boarded_at IS NOT NULL AND m.disembarked_at IS NULL", (vid,)).fetchone()):
                    continue
                place = c.execute("SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1", (vehicle["current_location_id"], world_id)).fetchone()
                if (place is None or place["longitude"] is None or place["latitude"] is None
                        or place["space_layer"] != vehicle["space_layer"]
                        or great_circle_distance_km(vehicle["longitude"], vehicle["latitude"], place["longitude"], place["latitude"]) > 0.02):
                    continue
                try:
                    confirmed = VehicleFuelInventoryService._vehicle_registration(c, world_id, vid, vehicle, at)
                except (ValueError, LookupError, TypeError):
                    continue
                original = c.execute("SELECT e.rowid AS event_order,e.* FROM world_events e WHERE e.world_id=? AND e.tick_id=? AND e.event_type='world.element_registered'", (world_id, vehicle["registration_id"])).fetchall()
                sources = []
                seen = set()
                for event in [*original, *recent]:
                    if (event["id"] in seen or event["location_id"] != place["id"]
                            or from_iso(event["occurred_at"]) > at
                            or not VehicleFuelInventoryService._event_not_after(confirmed["occurred_at"], confirmed["event_order"], event["occurred_at"], event["event_order"])):
                        continue
                    try:
                        payload = json.loads(event["payload_json"] or "{}")
                    except (ValueError, TypeError):
                        continue
                    if isinstance(payload, dict) and (payload.get("vehicle_id") == vid or (payload.get("entity_type") == "vehicle" and payload.get("entity_id") == vid)):
                        seen.add(event["id"])
                        sources.append({"id": event["id"], "occurred_at": event["occurred_at"], "location_id": place["id"]})
                if sources:
                    available.append({"id": vid, "name": vehicle["name"], "location_id": place["id"], "location_name": place["name"], "sources": sources})
            items = []
            if available:
                rows = c.execute(
                    "SELECT p.item_type_id,t.name,r.*,e.occurred_at,e.rowid AS source_event_order FROM world_item_profiles p "
                    "JOIN item_types t ON t.id=p.item_type_id "
                    "JOIN element_registration_requests r ON r.id=p.registration_id AND r.world_id=p.world_id "
                    "JOIN world_events e ON e.id=r.source_event_id AND e.world_id=r.world_id "
                    "WHERE p.world_id=? AND r.element_type='commodity' AND r.status='applied' "
                    "AND r.result_entity_id=p.item_type_id AND r.requested_by_character_id IS NULL ORDER BY t.name,t.id", (world_id,),
                ).fetchall()
                for row in rows:
                    try:
                        VehicleFuelInventoryService._confirmed(c, row, at, "item_type", row["item_type_id"])
                    except (ValueError, LookupError, TypeError):
                        continue
                    items.append({"id": row["item_type_id"], "name": row["name"]})
            return {"vehicles": available, "item_types": items, "recent_source_limit": 1000}

    return router
