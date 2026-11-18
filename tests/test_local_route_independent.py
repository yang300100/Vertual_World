"""哥哥，独立核验局部道路的明确确认、真实通行和关闭边界。"""

# ruff: noqa: F811

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_rides import boarded, choose as choose_ride, ride_world  # noqa: F401

from world_engine.local_routes import LocalRouteService
from world_engine.companion_proposals import CompanionIntent, CompanionProposalService
from world_engine.movement import MovementService, RouteUnavailable
from world_engine.navigation import TerrainService
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import WorldRepository, to_iso, utc_now


@pytest.fixture
def local_case(database, monkeypatch):
    repository = WorldRepository()
    registry = WorldElementRegistry()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="独立局部道路世界", minutes_per_tick=60, seed_demo=True,
            start_time=datetime(2030, 1, 1, 8, tzinfo=UTC),
        )
        snapshot = repository.get_snapshot(connection, world_id)
        place = snapshot.locations[0]
        player = repository.create_player_character(
            connection, world_id=world_id, name="独立行路者", identity="旅人",
            location_id=place.id, traits=[], goal="实地行路",
        )
        at = snapshot.world.current_time
        origin = (place.longitude - 0.0005, place.latitude)
        destination = (place.longitude + 0.0005, place.latitude)
        middle = (place.longitude, place.latitude + 0.0006)
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (*origin, player.id),
        )
        dataset_id, source_id = str(uuid4()), str(uuid4())
        connection.execute(
            "INSERT INTO navigation_datasets(id,world_id,name,asset_root,source_sha256,"
            "review_status,bounds_json,created_at,approved_at) "
            "VALUES (?,?,?,'independent-local-map','fixture','approved','{}',?,?)",
            (dataset_id, world_id, "独立已批准地形", to_iso(utc_now()), to_iso(utc_now())),
        )
        connection.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,location_id,"
            "summary,importance,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, world_id, str(uuid4()), to_iso(at), "world.local_route_surveyed",
             place.id, "作者已核对这条隔离道路的完整走向。", "routine", "{}", to_iso(utc_now())),
        )
    terrain = TerrainService.from_memory({
        1: {"cell_id": 1, "longitude": place.longitude, "latitude": place.latitude,
            "neighbors": [], "water_kind": "river"},
    })
    monkeypatch.setattr("world_engine.routing.load_terrain", lambda _root: terrain)
    payload = {
        "element_type": "local_route", "origin_mode": "surveyed_existing",
        "name": "经作者核对的弯曲通路", "dataset_id": dataset_id,
        "location_id": place.id, "polyline": [origin, middle, destination],
        "allowed_modes": ["land"], "bidirectional": True, "speed_limit_kmh": 5.0,
    }
    return SimpleNamespace(
        database=database, world_id=world_id, player_id=player.id, at=at,
        npc_id=snapshot.characters[0].id,
        dataset_id=dataset_id, source_id=source_id, registry=registry,
        payload=payload, origin=origin, destination=destination,
    )


def submit(case, connection, requester=None):
    request = ElementRegistrationSubmit.model_validate({
        "requested_by_character_id": requester,
        "source_event_id": case.source_id,
        "idempotency_key": "independent-local-route:" + str(uuid4()),
        "payload": case.payload,
    })
    return case.registry.submit(connection, world_id=case.world_id, request=request)


def position(connection, character_id):
    return tuple(connection.execute(
        "SELECT longitude,latitude FROM characters WHERE id=?", (character_id,),
    ).fetchone())


def start(case, connection):
    return MovementService().start(
        connection, world_id=case.world_id, character_id=case.player_id,
        destination_longitude=case.destination[0], destination_latitude=case.destination[1],
        world_time=case.at,
    )


def change_open(case, connection, route_id, at, is_open):
    source_id = str(uuid4())
    connection.execute(
        "UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), case.world_id),
    )
    connection.execute(
        "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,location_id,"
        "summary,importance,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, case.world_id, str(uuid4()), to_iso(at), "world.local_route_reviewed",
         case.payload["location_id"], "作者核对隔离道路的当前开放情况。", "routine", "{}", to_iso(utc_now())),
    )
    before = LocalRouteService.get(connection, case.world_id, route_id)
    changed = LocalRouteService.set_open(
        connection, case.world_id, route_id, is_open=is_open,
        expected_state_version=before["state_version"], source_event_id=source_id,
        requested_by_character_id=None,
    )
    return changed, source_id


@pytest.fixture
def approach_case(local_case, monkeypatch):
    case = local_case
    lon, lat = case.payload["polyline"][1]
    lat = case.origin[1]
    case.origin = (lon - 0.0025, lat)
    case.destination = (lon + 0.0025, lat)
    case.payload["polyline"] = [
        (lon - 0.0015, lat), (lon, lat + 0.0006), (lon + 0.0015, lat),
    ]
    with case.database.write() as connection:
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (*case.origin, case.player_id),
        )
    terrain = TerrainService.from_memory({
        1: {"cell_id": 1, "longitude": lon - 0.002, "latitude": lat,
            "neighbors": [2], "water_kind": None},
        2: {"cell_id": 2, "longitude": lon, "latitude": lat,
            "neighbors": [1, 3], "water_kind": "river"},
        3: {"cell_id": 3, "longitude": lon + 0.002, "latitude": lat,
            "neighbors": [2], "water_kind": None},
    })
    monkeypatch.setattr("world_engine.routing.load_terrain", lambda _root: terrain)
    return case


def test_candidate_does_not_open_path_or_move_person(local_case):
    case = local_case
    with case.database.write() as connection:
        candidate = submit(case, connection)
        assert candidate.status == "proposed"
        assert not LocalRouteService.available(
            connection, case.world_id, case.dataset_id, "land",
        )
        with pytest.raises(RouteUnavailable):
            start(case, connection)
        assert position(connection, case.player_id) == case.origin
        assert connection.execute(
            "SELECT COUNT(*) FROM character_movements WHERE world_id=?", (case.world_id,),
        ).fetchone()[0] == 0


def test_person_submission_cannot_confirm_world_geometry(local_case):
    case = local_case
    with case.database.write() as connection:
        candidate = submit(case, connection, requester=case.player_id)
        result = case.registry.confirm(
            connection, world_id=case.world_id, registration_id=candidate.id,
        )
        assert result.status == "rejected"
        assert not LocalRouteService.available(
            connection, case.world_id, case.dataset_id, "land",
        )
        assert position(connection, case.player_id) == case.origin


def test_author_confirmation_enables_real_segment_movement(local_case):
    case = local_case
    with case.database.write() as connection:
        candidate = submit(case, connection)
        result = case.registry.confirm(
            connection, world_id=case.world_id, registration_id=candidate.id,
        )
        assert result.result_entity_id
        movement = start(case, connection)
        route = json.loads(connection.execute(
            "SELECT route_json FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()[0])
        assert any(edge.get("local_route_id") == result.result_entity_id for edge in route["edges"])
        halfway = case.at + (movement.estimated_arrival_world - case.at) / 2
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=case.at,
            current_time=halfway, created_at=utc_now(),
        )
        actual = position(connection, case.player_id)
        assert actual != case.origin and actual != case.destination
        assert actual[1] > case.origin[1]
        assert connection.execute(
            "SELECT status FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()[0] == "moving"
        assert not connection.execute(
            "SELECT 1 FROM world_events WHERE event_type='action.movement_arrived' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement.id,),
        ).fetchone()
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=halfway,
            current_time=movement.estimated_arrival_world + timedelta(seconds=1), created_at=utc_now(),
        )
        assert position(connection, case.player_id) == case.destination
        assert connection.execute(
            "SELECT status FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()[0] == "arrived"


def test_closing_occupied_path_stops_at_actual_position_without_early_knowledge(local_case):
    case = local_case
    with case.database.write() as connection:
        candidate = submit(case, connection)
        confirmed = case.registry.confirm(
            connection, world_id=case.world_id, registration_id=candidate.id,
        )
        movement = start(case, connection)
        halfway = case.at + (movement.estimated_arrival_world - case.at) / 2
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=case.at,
            current_time=halfway, created_at=utc_now(),
        )
        before_position = position(connection, case.player_id)
        closed, close_source = change_open(
            case, connection, confirmed.result_entity_id, halfway, False,
        )
        assert closed["is_open"] == 0 or closed["is_open"] is False
        assert not connection.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id IN (?,?)",
            (case.player_id, close_source, closed["state_event_id"]),
        ).fetchone()
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=halfway,
            current_time=movement.estimated_arrival_world + timedelta(minutes=1), created_at=utc_now(),
        )
        assert position(connection, case.player_id) == before_position
        saved = connection.execute(
            "SELECT status,replan_reason FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()
        assert saved["status"] == "cancelled" and saved["replan_reason"]
        assert not connection.execute(
            "SELECT 1 FROM world_events WHERE event_type='action.movement_arrived' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement.id,),
        ).fetchone()


def test_distant_closed_path_is_only_encountered_at_its_actual_entrance(approach_case):
    case = approach_case
    with case.database.write() as connection:
        candidate = submit(case, connection)
        confirmed = case.registry.confirm(
            connection, world_id=case.world_id, registration_id=candidate.id,
        )
        movement = start(case, connection)
        route = json.loads(connection.execute(
            "SELECT route_json FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()[0])
        tagged = [index for index, edge in enumerate(route["edges"]) if edge.get("local_route_id")]
        assert tagged and tagged[0] > 0
        approach_hours = sum(
            edge["length_km"] / edge["speed_kmh"] for edge in route["edges"][:tagged[0]]
        )
        closed, source_id = change_open(
            case, connection, confirmed.result_entity_id, case.at, False,
        )
        before_entrance = case.at + timedelta(hours=approach_hours / 2)
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=case.at,
            current_time=before_entrance, created_at=utc_now(),
        )
        assert position(connection, case.player_id) != case.origin
        assert connection.execute(
            "SELECT status FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()[0] == "moving"
        assert not connection.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id IN (?,?)",
            (case.player_id, source_id, closed["state_event_id"]),
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM world_events WHERE event_type='action.movement_cancelled' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement.id,),
        ).fetchone()
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=before_entrance,
            current_time=movement.estimated_arrival_world + timedelta(minutes=1), created_at=utc_now(),
        )
        actual = position(connection, case.player_id)
        entrance = case.payload["polyline"][0]
        assert actual == tuple(entrance)
        assert connection.execute(
            "SELECT status FROM character_movements WHERE id=?", (movement.id,),
        ).fetchone()[0] == "cancelled"
        assert not connection.execute(
            "SELECT 1 FROM world_events WHERE event_type='action.movement_arrived' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement.id,),
        ).fetchone()


def test_bound_walkers_stop_at_the_same_actual_moment(approach_case):
    case = approach_case
    with case.database.write() as connection:
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=?,location_id=?,current_location_id=?,"
            "health=100,energy=95,satiety=95,movement_speed_kmh=5,identity='旅人' WHERE id=?",
            (case.origin[0] + 0.00002, case.origin[1], case.payload["location_id"],
             case.payload["location_id"], case.npc_id),
        )
        player = connection.execute("SELECT * FROM characters WHERE id=?", (case.player_id,)).fetchone()
        npc = connection.execute("SELECT * FROM characters WHERE id=?", (case.npc_id,)).fetchone()
        proposal = CompanionProposalService.apply(
            connection, player, npc, case.at,
            CompanionIntent(operation="propose", minutes=60, statement="我愿意共同沿这条通路行走。"),
        )
        CompanionProposalService.apply(
            connection, npc, player, case.at,
            CompanionIntent(operation="accept", proposal_id=proposal["proposal_id"],
                            statement="我本人明确接受这段时长的同行。"),
        )
        candidate = submit(case, connection)
        confirmed = case.registry.confirm(
            connection, world_id=case.world_id, registration_id=candidate.id,
        )
        start(case, connection)
        movements = connection.execute(
            "SELECT * FROM character_movements WHERE world_id=? AND status='moving'",
            (case.world_id,),
        ).fetchall()
        assert {row["character_id"] for row in movements} == {case.player_id, case.npc_id}
        seconds_to_entrance = []
        for movement in movements:
            route = json.loads(movement["route_json"])
            first_local = next(index for index, edge in enumerate(route["edges"]) if edge.get("local_route_id"))
            seconds_to_entrance.append(3600 * sum(
                edge["length_km"] / edge["speed_kmh"] for edge in route["edges"][:first_local]
            ))
        elapsed_seconds = min(seconds_to_entrance)
        expected = {
            row["character_id"]: MovementService()._advance_one(row, elapsed_seconds / 3600)[1:3]
            for row in movements
        }
        change_open(case, connection, confirmed.result_entity_id, case.at, False)
        MovementService().advance(
            connection, world_id=case.world_id, previous_time=case.at,
            current_time=case.at + timedelta(minutes=30), created_at=utc_now(),
        )
        for row in movements:
            assert position(connection, row["character_id"]) == pytest.approx(expected[row["character_id"]])
            assert connection.execute(
                "SELECT status FROM character_movements WHERE id=?", (row["id"],),
            ).fetchone()[0] == "cancelled"
            event = connection.execute(
                "SELECT occurred_at FROM world_events WHERE event_type='action.movement_cancelled' "
                "AND json_extract(payload_json,'$.movement_id')=?", (row["id"],),
            ).fetchone()
            assert event and event[0] == to_iso(case.at + timedelta(seconds=elapsed_seconds))
            assert not connection.execute(
                "SELECT 1 FROM world_events WHERE event_type='action.movement_arrived' "
                "AND json_extract(payload_json,'$.movement_id')=?", (row["id"],),
            ).fetchone()


def test_closed_path_keeps_real_vehicle_and_three_boarded_people(ride_world, monkeypatch):
    environment = ride_world
    ride_id = boarded(environment, member_ids=[environment.player, environment.npc, environment.other])
    with environment.db.write() as connection:
        origin = tuple(connection.execute(
            "SELECT longitude,latitude FROM vehicle_states WHERE vehicle_id=?", (environment.vehicle,),
        ).fetchone())
        destination = tuple(connection.execute(
            "SELECT longitude,latitude FROM locations WHERE id=?", (environment.destination,),
        ).fetchone())
        dataset_id, source_id = str(uuid4()), str(uuid4())
        connection.execute(
            "INSERT INTO navigation_datasets(id,world_id,name,asset_root,source_sha256,"
            "review_status,bounds_json,created_at,approved_at) "
            "VALUES (?,?,?,'independent-local-map','fixture','approved','{}',?,?)",
            (dataset_id, environment.wid, "共乘隔离地形", to_iso(utc_now()), to_iso(utc_now())),
        )
        connection.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,location_id,"
            "summary,importance,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, environment.wid, str(uuid4()), to_iso(environment.at), "world.local_route_surveyed",
             environment.origin, "作者核对了隔离车辆道路。", "routine", "{}", to_iso(utc_now())),
        )
        case = SimpleNamespace(
            database=environment.db, world_id=environment.wid, player_id=environment.player,
            at=environment.at, dataset_id=dataset_id, source_id=source_id,
            registry=WorldElementRegistry(), origin=origin, destination=destination,
            payload={
                "element_type": "local_route", "origin_mode": "surveyed_existing",
                "name": "隔离车辆弯路", "dataset_id": dataset_id, "location_id": environment.origin,
                "polyline": [origin, ((origin[0] + destination[0]) / 2, origin[1] + 0.001), destination],
                "allowed_modes": ["land"], "bidirectional": True, "speed_limit_kmh": 5.0,
            },
        )
        candidate = submit(case, connection)
        confirmed = case.registry.confirm(
            connection, world_id=case.world_id, registration_id=candidate.id,
        )
    terrain = TerrainService.from_memory({
        1: {"cell_id": 1, "longitude": origin[0], "latitude": origin[1],
            "neighbors": [], "water_kind": "river"},
    })
    monkeypatch.setattr("world_engine.routing.load_terrain", lambda _root: terrain)
    choose_ride(environment, environment.player, "depart", ride_id)
    with environment.db.write() as connection:
        movement = connection.execute(
            "SELECT m.* FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?",
            (ride_id,),
        ).fetchone()
        from world_engine.repository import from_iso

        halfway = environment.at + (from_iso(movement["estimated_arrival_world"]) - environment.at) / 2
        MovementService().advance(
            connection, world_id=environment.wid, previous_time=environment.at,
            current_time=halfway, created_at=utc_now(),
        )
        people = (environment.player, environment.npc, environment.other)
        positions_before = {person: position(connection, person) for person in people}
        vehicle_before = tuple(connection.execute(
            "SELECT longitude,latitude,distance_travelled_km FROM vehicle_states WHERE vehicle_id=?",
            (environment.vehicle,),
        ).fetchone())
        change_open(case, connection, confirmed.result_entity_id, halfway, False)
        MovementService().advance(
            connection, world_id=environment.wid, previous_time=halfway,
            current_time=from_iso(movement["estimated_arrival_world"]) + timedelta(minutes=1), created_at=utc_now(),
        )
        assert {person: position(connection, person) for person in people} == positions_before
        assert tuple(connection.execute(
            "SELECT longitude,latitude,distance_travelled_km FROM vehicle_states WHERE vehicle_id=?",
            (environment.vehicle,),
        ).fetchone()) == vehicle_before
        assert connection.execute(
            "SELECT status FROM vehicle_rides WHERE id=?", (ride_id,),
        ).fetchone()[0] == "blocked"
        assert connection.execute(
            "SELECT status FROM character_movements WHERE id=?", (movement["id"],),
        ).fetchone()[0] == "suspended"
        assert connection.execute(
            "SELECT COUNT(*) FROM vehicle_ride_members WHERE ride_id=? AND live=1 AND boarded_at IS NOT NULL "
            "AND disembarked_at IS NULL", (ride_id,),
        ).fetchone()[0] == 3
        assert not connection.execute(
            "SELECT 1 FROM world_events WHERE event_type='action.movement_arrived' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement["id"],),
        ).fetchone()


def test_management_api_chain_keeps_unexpressed_author_facts_out_of_player_experience(local_case, settings):
    from world_engine.api import create_app

    case = local_case
    base = f"/api/worlds/{case.world_id}"
    with TestClient(create_app(settings)) as client:
        options = client.get(base + "/local-routes/options")
        assert options.status_code == 200
        assert case.dataset_id in {item["id"] for item in options.json()["datasets"]}
        place = next(item for item in options.json()["locations"] if item["id"] == case.payload["location_id"])
        assert case.source_id in {item["id"] for item in place["sources"]}
        request = {
            "requested_by_character_id": None, "source_event_id": case.source_id,
            "idempotency_key": "independent-normal-api-local-route", "payload": case.payload,
        }
        response = client.post(base + "/registrations", json=request)
        assert response.status_code == 201, response.text
        proposal = response.json()
        assert proposal["status"] == "proposed"
        assert client.get(base + "/local-routes").json() == []
        confirmed = client.post(base + f"/registrations/{proposal['id']}/confirm")
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "applied"
        route_id = confirmed.json()["result_entity_id"]
        route = client.get(base + f"/local-routes/{route_id}").json()
        body = {
            "is_open": False, "expected_state_version": route["state_version"],
            "source_event_id": case.source_id, "requested_by_character_id": case.player_id,
        }
        refused = client.patch(base + f"/local-routes/{route_id}/state", json=body)
        assert refused.status_code == 409
        assert client.get(base + f"/local-routes/{route_id}").json()["is_open"] is True
        body["requested_by_character_id"] = None
        closed = client.patch(base + f"/local-routes/{route_id}/state", json=body)
        assert closed.status_code == 200, closed.text
        assert closed.json()["is_open"] is False
        experience = client.get(base + "/player/experience")
        assert experience.status_code == 200, experience.text
        assert route["registered_event_id"] not in experience.text
        assert closed.json()["state_event_id"] not in experience.text
        with case.database.read() as connection:
            assert not connection.execute(
                "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id IN (?,?)",
                (case.player_id, route["registered_event_id"], closed.json()["state_event_id"]),
            ).fetchone()
