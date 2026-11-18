"""哥哥，仍有导航标记的损坏路线不会回退直线。"""

import json
from datetime import timedelta

import pytest

from world_engine.geo import great_circle_distance_km
from world_engine.movement import MovementService
from world_engine.repository import WorldRepository, to_iso, utc_now
from world_engine.routing import RoutePlan


def start_route(database, monkeypatch):
    repo = WorldRepository()
    with database.write() as c:
        wid = repo.create_world(c, name="隔离导航路线", minutes_per_tick=60, seed_demo=True)
        snapshot = repo.get_snapshot(c, wid)
        player = repo.create_player_character(c, world_id=wid, name="旅人", identity="旅人",
                                              location_id=snapshot.locations[0].id, traits=[], goal="走路")
        at = snapshot.world.current_time

        def plan(_self, _connection, **kwargs):
            actor = kwargs["actor"]
            origin = (float(actor["longitude"]), float(actor["latitude"]))
            destination = (kwargs["destination_longitude"], kwargs["destination_latitude"])
            middle = ((origin[0] + destination[0]) / 2, origin[1] + 0.002)
            points = [list(origin), list(middle), list(destination)]
            edges = [{"length_km": round(great_circle_distance_km(*a, *b), 4), "speed_kmh": 5.0}
                     for a, b in zip(points, points[1:])]
            return RoutePlan(reachable=True, origin=origin, destination=destination,
                             movement_type=kwargs["movement_type"], polyline=points, edges=edges,
                             distance_km=round(sum(e["length_km"] for e in edges), 3),
                             estimated_hours=round(sum(e["length_km"] / e["speed_kmh"] for e in edges), 3),
                             dataset_id="isolated-approved-dataset", dataset_status="approved")

        monkeypatch.setattr(MovementService, "_plan_route", plan)
        movement = MovementService().start(c, world_id=wid, character_id=player.id,
                                           destination_longitude=player.longitude + 0.01,
                                           destination_latitude=player.latitude, world_time=at)
    return wid, player.id, at, movement


@pytest.mark.parametrize("corruption", ["missing", "bad_json", "missing_edges", "wrong_dataset", "missing_binding", "ambiguous_unbound", "binding_and_route", "nan_speed",
                                        "wrong_length", "wrong_index", "wrong_position"])
def test_invalid_navigation_route_stops_before_eta_without_false_arrival(database, monkeypatch, corruption):
    wid, player, at, movement = start_route(database, monkeypatch)
    with database.write() as c:
        data = json.loads(c.execute("SELECT route_json FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0])
        if corruption == "missing":
            raw = "[]"
        elif corruption == "bad_json":
            raw = "{"
        elif corruption == "binding_and_route":
            c.execute("UPDATE character_movements SET navigation_dataset_id=NULL WHERE id=?", (movement.id,))
            raw = "[]"
        else:
            if corruption == "missing_edges":
                data["edges"] = []
            elif corruption == "wrong_dataset":
                data["dataset_id"] = "other-dataset"
            elif corruption == "missing_binding":
                c.execute("UPDATE character_movements SET navigation_dataset_id=NULL WHERE id=?", (movement.id,))
            elif corruption == "ambiguous_unbound":
                c.execute("UPDATE character_movements SET navigation_dataset_id=NULL WHERE id=?", (movement.id,))
                data["dataset_id"] = None
            elif corruption == "nan_speed":
                data["edges"][0]["speed_kmh"] = float("nan")
            elif corruption == "wrong_length":
                data["edges"][0]["length_km"] = 99
            elif corruption == "wrong_index":
                c.execute("UPDATE character_movements SET route_index=? WHERE id=?", (len(data["edges"]), movement.id))
            elif corruption == "wrong_position":
                c.execute("UPDATE characters SET latitude=latitude+0.01 WHERE id=?", (player,))
            raw = json.dumps(data)
        c.execute("UPDATE character_movements SET route_json=? WHERE id=?", (raw, movement.id))
        before = tuple(c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone())
        end = movement.estimated_arrival_world + timedelta(seconds=5)
        MovementService().advance(c, world_id=wid, previous_time=at, current_time=end, created_at=utc_now())
        after = tuple(c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone())
        saved = c.execute("SELECT status,replan_reason FROM character_movements WHERE id=?", (movement.id,)).fetchone()
        assert after == before
        assert saved["status"] == "cancelled" and saved["replan_reason"]
        assert not c.execute("SELECT 1 FROM world_events WHERE actor_id=? AND event_type='action.movement_arrived'", (player,)).fetchone()


def test_valid_navigation_finishes_at_its_calculated_eta(database, monkeypatch):
    wid, player, at, movement = start_route(database, monkeypatch)
    with database.write() as c:
        MovementService().advance(c, world_id=wid, previous_time=at,
                                  current_time=movement.estimated_arrival_world, created_at=utc_now())
        saved = c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()
        person = c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone()
        assert saved["status"] == "arrived"
        assert (person["longitude"], person["latitude"]) == (movement.destination_longitude, movement.destination_latitude)


def test_old_unbound_route_retains_legacy_interpolation(database, monkeypatch):
    wid, player, at, movement = start_route(database, monkeypatch)
    with database.write() as c:
        c.execute("UPDATE character_movements SET navigation_dataset_id=NULL,route_distance_km=0,route_json='[]' WHERE id=?", (movement.id,))
        before = tuple(c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone())
        MovementService().advance(c, world_id=wid, previous_time=at,
                                  current_time=at + timedelta(seconds=10), created_at=utc_now())
        after = tuple(c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone())
        assert after != before
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0] == "moving"


@pytest.mark.parametrize("delta", [-5, 5])
def test_existing_navigation_uses_actual_segments_even_when_legacy_eta_was_rounded(database, monkeypatch, delta):
    wid, player, at, movement = start_route(database, monkeypatch)
    actual_end = movement.estimated_arrival_world
    with database.write() as c:
        old_eta = actual_end + timedelta(seconds=delta)
        c.execute("UPDATE character_movements SET estimated_arrival_world=? WHERE id=?", (to_iso(old_eta), movement.id))
        first_end = min(old_eta, actual_end)
        MovementService().advance(c, world_id=wid, previous_time=at, current_time=first_end, created_at=utc_now())
        saved = c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0]
        if delta < 0:
            assert saved == "moving"
            assert tuple(c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone()) != (movement.destination_longitude, movement.destination_latitude)
            MovementService().advance(c, world_id=wid, previous_time=first_end, current_time=actual_end, created_at=utc_now())
            saved = c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0]
        assert saved == "arrived"
        assert tuple(c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone()) == (movement.destination_longitude, movement.destination_latitude)
