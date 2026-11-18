"""哥哥，共乘候选只来自本人眼前的真实车辆和已知人物、地点。"""

from world_engine.contracts import ContractService
from world_engine.geo import great_circle_distance_km
from world_engine.group_journey_options import group_journey_options


def ride_options(c, world_id, character_id, at):
    actor = c.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (character_id, world_id)).fetchone()
    base = group_journey_options(c, world_id, character_id, at)
    result = {**base, "vehicles": []}
    if actor is None or actor["current_room_id"] or actor["longitude"] is None or actor["latitude"] is None:
        return result
    visible = {person["id"] for person in base["members"]} | {character_id}
    for vehicle in c.execute(
        "SELECT v.*,s.capacity,s.longitude,s.latitude,s.space_layer,s.current_location_id,s.status "
        "FROM vehicles v JOIN vehicle_states s ON s.vehicle_id=v.id AND s.world_id=v.world_id "
        "WHERE v.world_id=? AND v.is_available=1 AND s.space_layer=? AND s.status='parked'",
        (world_id, actor["space_layer"]),
    ).fetchall():
        if great_circle_distance_km(actor["longitude"], actor["latitude"], vehicle["longitude"], vehicle["latitude"]) > 0.1:
            continue
        controllers = [cid for cid in sorted(visible) if ContractService.vehicle_for(c, world_id, cid, vehicle["id"]) is not None]
        if not controllers:
            continue
        result["vehicles"].append({key: vehicle[key] for key in (
            "id", "name", "capacity", "current_location_id", "status", "longitude", "latitude", "space_layer",
        )} | {"controller_id": controllers[0], "controller_ids": controllers})
    return result
