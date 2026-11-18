"""哥哥，途中交谈只沿用正在共同走的真实一程，不把可见陌生人当成同伴。"""

import json

from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso


class TravelInteractionService:
    JOB_PREFIX = "共同移动中的交谈机会"

    @staticmethod
    def _started(c, wid, movement, at):
        for event in c.execute(
            "SELECT * FROM world_events WHERE world_id=? AND actor_id=? "
            "AND event_type='action.movement_started' AND occurred_at=? "
            "ORDER BY rowid DESC LIMIT 8",
            (wid, movement["character_id"], movement["started_at_world"]),
        ):
            try:
                if (
                    json.loads(event["payload_json"]).get("movement_id") == movement["id"]
                    and from_iso(event["occurred_at"]) <= at
                ):
                    return event
            except (AttributeError, TypeError, ValueError):
                continue
        return None

    @classmethod
    def shared_motion(cls, c, wid, npc_id, player_id, at):
        people = {
            row["id"]: row
            for row in c.execute(
                "SELECT * FROM characters WHERE world_id=? AND id IN (?,?)",
                (wid, npc_id, player_id),
            )
        }
        if len(people) != 2:
            return None
        npc, player = people[npc_id], people[player_id]
        if (
            npc["is_player"]
            or not player["is_player"]
            or npc["health"] <= 0
            or player["health"] <= 0
            or npc["current_room_id"]
            or player["current_room_id"]
            or not same_room(npc, player)
            or great_circle_distance_km(
                npc["longitude"], npc["latitude"], player["longitude"], player["latitude"]
            )
            > 0.02
        ):
            return None
        found = c.execute(
            "SELECT * FROM character_movements WHERE world_id=? "
            "AND character_id IN (?,?) AND status='moving'",
            (wid, npc_id, player_id),
        ).fetchall()
        movements = {row["character_id"]: row for row in found}
        if len(found) != 2 or set(movements) != {npc_id, player_id}:
            return None
        for movement in movements.values():
            if (
                movement["vehicle_id"]
                or not from_iso(movement["started_at_world"]) <= at
                or at >= from_iso(movement["estimated_arrival_world"])
            ):
                return None
        started = {cid: cls._started(c, wid, movement, at) for cid, movement in movements.items()}
        if not all(started.values()):
            return None
        binding = {
            "npc_movement_id": movements[npc_id]["id"],
            "player_movement_id": movements[player_id]["id"],
            "in_motion": True,
            "has_arrived": False,
            "talk_range_m": 20,
        }
        for leg in c.execute(
            "SELECT g.*,j.deadline_at,j.proposer_id,j.recipient_id FROM journey_legs g "
            "JOIN joint_journeys j ON j.id=g.journey_id WHERE j.world_id=? AND j.status='active' "
            "AND j.phase='moving' AND g.status='moving' AND g.stop_index=j.next_index "
            "AND ((j.proposer_id=? AND j.recipient_id=?) "
            "OR (j.proposer_id=? AND j.recipient_id=?))",
            (wid, npc_id, player_id, player_id, npc_id),
        ):
            if {leg["player_movement_id"], leg["npc_movement_id"]} != {
                m["id"] for m in movements.values()
            } or at >= from_iso(leg["deadline_at"]):
                continue
            event = c.execute(
                "SELECT * FROM world_events WHERE world_id=? AND id=?",
                (wid, leg["source_event_id"]),
            ).fetchone()
            try:
                payload = json.loads(event["payload_json"]) if event else {}
                valid = bool(
                    event
                    and event["event_type"] == "social.appointment_journey_departed"
                    and {event["actor_id"], event["target_id"]} == {npc_id, player_id}
                    and payload.get("journey_id") == leg["journey_id"]
                    and payload.get("leg_id") == leg["id"]
                    and set(payload.get("movement_ids", []))
                    == {m["id"] for m in movements.values()}
                    and from_iso(event["occurred_at"]) == from_iso(leg["started_at"]) <= at
                )
            except (AttributeError, TypeError, ValueError):
                valid = False
            if valid:
                return {
                    **binding,
                    "kind": "journey",
                    "journey_id": leg["journey_id"],
                    "leg_id": leg["id"],
                    "source_event_id": leg["source_event_id"],
                }
        from world_engine.group_journeys import GroupJourneyService

        for row in c.execute(
            "SELECT g.* FROM group_journeys g "
            "JOIN group_journey_members n ON n.journey_id=g.id AND n.character_id=? AND n.left_at IS NULL "
            "JOIN group_journey_members p ON p.journey_id=g.id AND p.character_id=? AND p.left_at IS NULL "
            "WHERE g.world_id=? AND g.status='active' AND g.phase='moving' AND g.deadline_at>?",
            (npc_id, player_id, wid, to_iso(at)),
        ):
            leg = c.execute("SELECT * FROM group_journey_legs WHERE journey_id=? AND status='moving' AND stop_index=?",
                            (row["id"], row["next_index"])).fetchone()
            if not leg or not GroupJourneyService._accepted(c, row, at) or not GroupJourneyService._resumed(c, row, at):
                continue
            if GroupJourneyService._follow_required(c, row) and not GroupJourneyService.following(c, row, at):
                continue
            if not GroupJourneyService._leg_valid(c, row, leg, at):
                continue
            links = {link["character_id"]: link["movement_id"] for link in c.execute(
                "SELECT character_id,movement_id FROM group_journey_leg_members WHERE leg_id=?", (leg["id"],))}
            if any(links.get(cid) != movement["id"] for cid, movement in movements.items()):
                continue
            return {**binding, "kind": "group_journey", "journey_id": row["id"],
                    "leg_id": leg["id"], "source_event_id": leg["source_event_id"]}
        session = c.execute(
            "SELECT * FROM companion_sessions WHERE world_id=? "
            "AND status='active' AND until_world_time>? "
            "AND ((leader_id=? AND companion_id=?) OR (leader_id=? AND companion_id=?))",
            (wid, to_iso(at), npc_id, player_id, player_id, npc_id),
        ).fetchone()
        if not session or from_iso(session["started_world_time"]) > at:
            return None
        first, second = movements[npc_id], movements[player_id]
        if (
            from_iso(first["started_at_world"]) != from_iso(second["started_at_world"])
            or from_iso(first["estimated_arrival_world"])
            != from_iso(second["estimated_arrival_world"])
            or any(
                abs(first[key] - second[key]) > 1e-9
                for key in ("destination_longitude", "destination_latitude")
            )
        ):
            return None
        # 哥哥，来源用 NPC 本人这一程的真实出发，下一程不能沿用上一次的联系机会。
        return {
            **binding,
            "kind": "companion",
            "session_id": session["id"],
            "source_event_id": started[npc_id]["id"],
        }

    @classmethod
    def is_travel_job(cls, job):
        return bool(job["reason"] and job["reason"].startswith(cls.JOB_PREFIX))

    @classmethod
    def for_job(cls, c, job, at):
        if not cls.is_travel_job(job) or job["channel"] != "in_person":
            return None
        current = cls.shared_motion(c, job["world_id"], job["npc_id"], job["player_id"], at)
        return current if current and current["source_event_id"] == job["source_event_id"] else None

    @classmethod
    def current_job(cls, c, job, at):
        return not cls.is_travel_job(job) or cls.for_job(c, job, at) is not None

    @staticmethod
    def context(binding):
        if not binding:
            return None
        result = {
            key: value
            for key, value in binding.items()
            if key not in {"npc_movement_id", "player_movement_id"}
        }
        result["scope"] = (
            "双方仍在真实同行的一程中，可以自愿说话或提出暂停；还未到站，改变路线仍须另外商量。"
        )
        return result
