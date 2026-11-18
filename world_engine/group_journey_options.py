"""哥哥，组团候选只展示本人眼前的人和真正到访过的地点。"""

from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.repository import to_iso


def group_journey_options(c, world_id, character_id, at):
    from world_engine.society import SocietyService

    actor = c.execute(
        "SELECT * FROM characters WHERE id=? AND world_id=?", (character_id, world_id)
    ).fetchone()
    if actor is None:
        return {"members": [], "places": [], "player_id": None}
    people = [{"id": actor["id"], "name": "你", "is_player": bool(actor["is_player"])}] + [
        {
            "id": person["id"],
            "name": SocietyService.label(c, character_id, person["id"]),
            "is_player": bool(person["is_player"]),
        }
        for person in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND id<>? AND health>0 ORDER BY rowid",
            (world_id, character_id),
        )
        if same_room(actor, person)
        and great_circle_distance_km(
            actor["longitude"],
            actor["latitude"],
            person["longitude"],
            person["latitude"],
        )
        <= 0.02
    ]
    places = [
        dict(row)
        for row in c.execute(
            "SELECT l.id,l.name FROM locations l WHERE l.world_id=? AND l.is_active=1 AND l.space_layer=? "
            "AND (l.id=COALESCE(?,?) OR l.id IN (SELECT e.location_id FROM world_events e "
            "JOIN event_observers o ON o.event_id=e.id JOIN character_event_knowledge k "
            "ON k.event_id=e.id AND k.character_id=o.character_id AND k.world_id=e.world_id "
            "WHERE e.world_id=? AND o.character_id=? AND o.channel='participant' "
            "AND e.occurred_at<=? AND k.learned_world_time<=?)) ORDER BY l.name LIMIT 40",
            (
                world_id,
                actor["space_layer"],
                actor["current_location_id"],
                actor["location_id"],
                world_id,
                character_id,
                to_iso(at),
                to_iso(at),
            ),
        )
    ]
    player = c.execute(
        "SELECT id FROM characters WHERE world_id=? AND is_player=1", (world_id,)
    ).fetchone()
    return {"members": people, "places": places, "player_id": player[0] if player else None}
