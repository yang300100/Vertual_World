"""哥哥，只有编年者明确核实的局部通路才参与开放通路集合。"""

import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.local_routes import LocalRouteConflict, LocalRouteService
from world_engine.registration import ElementRegistrationSubmit, LocalRouteSpec, WorldElementRegistry
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now
from world_engine.database import SCHEMA
from world_engine import migrations


@pytest.fixture
def route_world(database):
    with database.write() as c:
        wid = WorldRepository().create_world(c, name="局部通路隔离世界", minutes_per_tick=60, seed_demo=True)
        location = dict(c.execute("SELECT * FROM locations WHERE world_id=? AND is_active=1 LIMIT 1", (wid,)).fetchone())
        actor = c.execute("SELECT id FROM characters WHERE world_id=? LIMIT 1", (wid,)).fetchone()[0]
        at = from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()[0])
        source, dataset = str(uuid4()), str(uuid4())
        c.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,actor_id,location_id,summary,importance,payload_json,created_at) "
            "VALUES (?,?,?,?,'action.registration_source',?,?,?,'routine','{}',?)",
            (source, wid, str(uuid4()), to_iso(at - timedelta(minutes=1)), actor, location["id"],
             "编年者核对既存道路的实际折线。", to_iso(utc_now())),
        )
        c.execute(
            "INSERT INTO navigation_datasets(id,world_id,name,asset_root,source_sha256,review_status,bounds_json,created_at) "
            "VALUES (?,?,?,'isolated','fixture','approved','{}',?)", (dataset, wid, "隔离已审核地图", to_iso(utc_now())),
        )
        return {"world_id": wid, "location": location, "actor_id": actor,
                "source_id": source, "dataset_id": dataset, "at": at}


def route_request(world, **changes):
    longitude, latitude = world["location"]["longitude"], world["location"]["latitude"]
    payload = {
        "element_type": "local_route", "origin_mode": "surveyed_existing", "name": "核实的岸边小路",
        "dataset_id": world["dataset_id"], "location_id": world["location"]["id"],
        "polyline": [[longitude, latitude], [longitude + 0.001, latitude]],
        "speed_limit_kmh": 4,
    }
    payload.update(changes)
    return ElementRegistrationSubmit.model_validate({
        "source_event_id": world["source_id"], "idempotency_key": f"local-route:{uuid4()}", "payload": payload,
    })


def confirmed(c, world):
    registry = WorldElementRegistry()
    proposal = registry.submit(c, world_id=world["world_id"], request=route_request(world))
    result = registry.confirm(c, world_id=world["world_id"], registration_id=proposal.id)
    assert result.status == "applied", result.rejection_reason
    return result


def test_proposal_waits_for_author_confirmation_and_preserves_people(database, route_world):
    world, registry = route_world, WorldElementRegistry()
    with database.write() as c:
        before = [tuple(row) for row in c.execute("SELECT id,longitude,latitude,money FROM characters WHERE world_id=? ORDER BY id", (world["world_id"],))]
        request = route_request(world)
        proposal = registry.submit(c, world_id=world["world_id"], request=request)
        assert proposal.status == "proposed"
        assert LocalRouteService.available(c, world["world_id"], world["dataset_id"], "land") == []
        assert c.execute("SELECT COUNT(*) FROM world_local_routes").fetchone()[0] == 0
        result = registry.confirm(c, world_id=world["world_id"], registration_id=proposal.id)
        assert result.status == "applied", result.rejection_reason
        path = LocalRouteService.get(c, world["world_id"], result.result_entity_id)
        assert path["polyline"] == request.payload.model_dump(mode="json")["polyline"]
        assert path["is_open"] and path["state_version"] == 1
        assert [item["id"] for item in LocalRouteService.available(c, world["world_id"], world["dataset_id"], "land")] == [path["id"]]
        assert registry.submit(c, world_id=world["world_id"], request=request).id == proposal.id
        assert before == [tuple(row) for row in c.execute("SELECT id,longitude,latitude,money FROM characters WHERE world_id=? ORDER BY id", (world["world_id"],))]


def test_character_request_cannot_be_confirmed_as_world_road(database, route_world):
    world, registry = route_world, WorldElementRegistry()
    request = route_request(world).model_copy(update={"requested_by_character_id": world["actor_id"]})
    with database.write() as c:
        proposal = registry.submit(c, world_id=world["world_id"], request=request)
        result = registry.confirm(c, world_id=world["world_id"], registration_id=proposal.id)
        assert result.status == "rejected" and "编年者" in result.rejection_reason
        assert c.execute("SELECT COUNT(*) FROM world_local_routes").fetchone()[0] == 0


@pytest.mark.parametrize("corruption", ["candidate", "other_world", "wrong_location", "future", "invalid_time", "inactive", "sky"])
def test_unverified_source_or_dataset_cannot_leave_partial_road(database, route_world, corruption):
    world = route_world
    with database.write() as c:
        if corruption == "candidate":
            c.execute("UPDATE navigation_datasets SET review_status='candidate' WHERE id=?", (world["dataset_id"],))
        elif corruption == "other_world":
            other = WorldRepository().create_world(c, name="其他世界", minutes_per_tick=60, seed_demo=True)
            c.execute("UPDATE navigation_datasets SET world_id=? WHERE id=?", (other, world["dataset_id"]))
        elif corruption == "wrong_location":
            c.execute("UPDATE world_events SET location_id=NULL WHERE id=?", (world["source_id"],))
        elif corruption == "future":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(world["at"] + timedelta(seconds=1)), world["source_id"]))
        elif corruption == "invalid_time":
            c.execute("UPDATE world_events SET occurred_at='损坏的时间' WHERE id=?", (world["source_id"],))
        elif corruption == "inactive":
            c.execute("UPDATE locations SET is_active=0 WHERE id=?", (world["location"]["id"],))
        else:
            c.execute("UPDATE locations SET space_layer='sky' WHERE id=?", (world["location"]["id"],))
        registry = WorldElementRegistry()
        proposal = registry.submit(c, world_id=world["world_id"], request=route_request(world))
        result = registry.confirm(c, world_id=world["world_id"], registration_id=proposal.id)
        assert result.status == "rejected"
        assert c.execute("SELECT COUNT(*) FROM world_local_routes").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE event_type='world.local_route_registered'").fetchone()[0] == 0


def test_state_uses_current_time_source_and_version(database, route_world):
    world = route_world
    with database.write() as c:
        result = confirmed(c, world)
        route_id = result.result_entity_id
        at = world["at"] + timedelta(minutes=2)
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), world["world_id"]))
        source = str(uuid4())
        c.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,location_id,summary,importance,payload_json,created_at) "
            "VALUES (?,?,?,?,'world.route_observed',?,?,'routine','{}',?)",
            (source, world["world_id"], str(uuid4()), to_iso(at), world["location"]["id"], "道路实际关闭。", to_iso(utc_now())),
        )
        closed = LocalRouteService.set_open(c, world["world_id"], route_id, is_open=False,
                                           expected_state_version=1, source_event_id=source)
        assert not closed["is_open"] and closed["state_version"] == 2
        assert closed["state_changed_at"] == to_iso(at)
        assert LocalRouteService.available(c, world["world_id"], world["dataset_id"], "land") == []
        assert LocalRouteService.set_open(c, world["world_id"], route_id, is_open=False,
                                         expected_state_version=1, source_event_id=source)["state_version"] == 2
        with pytest.raises(LocalRouteConflict):
            LocalRouteService.set_open(c, world["world_id"], route_id, is_open=True,
                                       expected_state_version=1, source_event_id=source)
        with pytest.raises(LocalRouteConflict):
            LocalRouteService.set_open(c, world["world_id"], route_id, is_open=True,
                                       expected_state_version=2, source_event_id=source,
                                       requested_by_character_id=world["actor_id"])
        reopened = LocalRouteService.set_open(c, world["world_id"], route_id, is_open=True,
                                              expected_state_version=2, source_event_id=source)
        assert reopened["is_open"] and reopened["state_version"] == 3


@pytest.mark.parametrize("column,value", [
    ("polyline_json", "[[0,0],[0.01,0]]"), ("speed_limit_kmh", 99), ("revision", 1),
    ("registration_id", None),
])
def test_available_rechecks_source_and_immutable_geometry(database, route_world, column, value):
    world = route_world
    with database.write() as c:
        result = confirmed(c, world)
        if column == "revision":
            c.execute("UPDATE world_events SET payload_json='{}' WHERE id=(SELECT registered_event_id FROM world_local_routes WHERE id=?)", (result.result_entity_id,))
        elif column == "registration_id":
            c.execute("UPDATE element_registration_requests SET requested_by_character_id=? WHERE id=?", (world["actor_id"], result.id))
        else:
            c.execute(f"UPDATE world_local_routes SET {column}=? WHERE id=?", (value, result.result_entity_id))
        with pytest.raises(LocalRouteConflict):
            LocalRouteService.get(c, world["world_id"], result.result_entity_id)
        assert LocalRouteService.available(c, world["world_id"], world["dataset_id"], "land") == []


@pytest.mark.parametrize("points", [[], [[0, 0]], [[0, 0], [0, 0]], [[0, 0], [181, 0]], [[0, 0], [0, float("inf")]]])
def test_geometry_is_finite_and_has_real_segments(points):
    with pytest.raises(ValidationError):
        LocalRouteSpec(name="几何边界", dataset_id="dataset", location_id="place", polyline=points, speed_limit_kmh=4)


def test_old_registration_constraint_migration_preserves_records_and_references():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA.replace(",'local_route'", ""))
        c.execute("INSERT INTO worlds(id,name,current_time,minutes_per_tick,created_at,updated_at) VALUES ('w','旧世界','2040-01-01T00:00:00+00:00',60,'a','a')")
        c.execute("INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,summary,created_at) VALUES ('e','w','t','2040-01-01T00:00:00+00:00','source','原来源','a')")
        c.execute("INSERT INTO element_registration_requests(id,world_id,element_type,source_event_id,idempotency_key,status,payload_json,input_world_version,created_at,updated_at) VALUES ('r','w','lore','e','old:local-route','applied','{}',1,'a','a')")
        c.execute("INSERT INTO world_entities(id,world_id,entity_type,entity_id,name,registration_id,created_at) VALUES ('entity','w','knowledge','kept','原记录','r','a')")
        c.execute("CREATE INDEX idx_route_legacy_payload ON element_registration_requests(payload_json)")
        c.execute("CREATE TABLE route_migration_audit(id TEXT)")
        c.execute("CREATE TRIGGER route_legacy_audit AFTER UPDATE OF status ON element_registration_requests BEGIN INSERT INTO route_migration_audit VALUES(new.id); END")
        c.commit()
        before = {name: [tuple(row) for row in c.execute(f"SELECT * FROM {name}")]
                  for name in ("worlds", "world_events", "element_registration_requests", "world_entities")}
        migrations.migrate_registration_element_types(c)
        assert before == {name: [tuple(row) for row in c.execute(f"SELECT * FROM {name}")] for name in before}
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_route_legacy_payload'").fetchone()
        c.execute("UPDATE element_registration_requests SET status='applied' WHERE id='r'")
        assert c.execute("SELECT id FROM route_migration_audit").fetchone()[0] == "r"
        c.execute("INSERT INTO element_registration_requests(id,world_id,element_type,source_event_id,idempotency_key,status,payload_json,input_world_version,created_at,updated_at) VALUES ('new','w','local_route','e','new:local-route','proposed','{}',1,'a','a')")
        version = c.execute("PRAGMA schema_version").fetchone()[0]
        migrations.migrate_registration_element_types(c)
        assert c.execute("PRAGMA schema_version").fetchone()[0] == version
    finally:
        c.close()


def test_management_api_options_and_state_use_existing_registration_flow(database, route_world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from world_engine.api_elements import build_elements_router

    world = route_world
    app = FastAPI()
    app.include_router(build_elements_router(database))
    api = TestClient(app)
    prefix = f"/api/worlds/{world['world_id']}"
    options = api.get(prefix + "/local-routes/options")
    assert options.status_code == 200
    assert options.json()["datasets"] == [{"id": world["dataset_id"], "name": "隔离已审核地图"}]
    place = next(row for row in options.json()["locations"] if row["id"] == world["location"]["id"])
    assert world["source_id"] in {row["id"] for row in place["sources"]}
    draft = api.post(prefix + "/registrations", json=route_request(world).model_dump(mode="json"))
    assert draft.status_code == 201 and draft.json()["status"] == "proposed"
    assert api.get(prefix + "/local-routes").json() == []
    applied = api.post(prefix + f"/registrations/{draft.json()['id']}/confirm")
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    path = api.get(prefix + "/local-routes").json()[0]
    denied = api.patch(prefix + f"/local-routes/{path['id']}/state", json={
        "is_open": False, "expected_state_version": 1, "source_event_id": world["source_id"],
        "requested_by_character_id": world["actor_id"],
    })
    assert denied.status_code == 409
    assert api.get(prefix + f"/local-routes/{path['id']}").json()["is_open"]
    with database.write() as c:
        bad_source = str(uuid4())
        c.execute(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,location_id,summary,importance,payload_json,created_at) "
            "VALUES (?,?,?,'损坏的时间','world.route_observed',?,?,'routine','{}',?)",
            (bad_source, world["world_id"], str(uuid4()), world["location"]["id"], "无法使用的时间记录。", to_iso(utc_now())),
        )
    invalid = api.patch(prefix + f"/local-routes/{path['id']}/state", json={
        "is_open": False, "expected_state_version": 1, "source_event_id": bad_source,
    })
    assert invalid.status_code == 409
    assert "时间无效" in invalid.json()["detail"]
    assert api.get(prefix + f"/local-routes/{path['id']}").json()["state_version"] == 1
