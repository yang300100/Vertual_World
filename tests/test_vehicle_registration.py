from __future__ import annotations

from datetime import timedelta
import sqlite3
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.registration import (
    ElementRegistrationSubmit,
    RegistrationConflict,
    VehicleSpec,
    WorldElementRegistry,
)
from world_engine import migrations
from world_engine.database import SCHEMA
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now


@pytest.fixture
def vehicle_world(database):
    with database.write() as c:
        wid = WorldRepository().create_world(c, name="共乘登记隔离世界", minutes_per_tick=60, seed_demo=True)
        actor = c.execute("SELECT * FROM characters WHERE world_id=? LIMIT 1", (wid,)).fetchone()
        location = c.execute("SELECT * FROM locations WHERE id=?", (actor["location_id"],)).fetchone()
        at = c.execute('SELECT w.current_time FROM worlds w WHERE w.id=?', (wid,)).fetchone()[0]
        source_id = str(uuid4())
        c.execute("""INSERT INTO world_events(
            id,world_id,tick_id,occurred_at,event_type,actor_id,location_id,
            summary,importance,payload_json,created_at
        ) VALUES (?,?,?,?, 'action.registration_source',?,?,?,'routine','{}',?)""",
            (source_id, wid, str(uuid4()), at, actor["id"], location["id"],
             "编年者正在核实地点上既有载具的实物与座位。", to_iso(utc_now())))
        return {"world_id": wid, "source_id": source_id, "actor_id": actor["id"],
                "location": dict(location), "at": from_iso(at)}


def request_for(world, **overrides):
    payload = {"element_type": "vehicle", "name": "河湾双座马车",
               "movement_type": "land", "speed_kmh": 12,
               "owner_character_id": world["actor_id"], "capacity": 2,
               "location_id": world["location"]["id"]}
    payload.update(overrides)
    return ElementRegistrationSubmit.model_validate({
        "requested_by_character_id": None, "source_event_id": world["source_id"],
        "idempotency_key": f"vehicle:{uuid4()}", "payload": payload,
    })


def register(c, world, **payload):
    registry = WorldElementRegistry()
    proposed = registry.submit(c, world_id=world["world_id"], request=request_for(world, **payload))
    return registry.confirm(c, world_id=world["world_id"], registration_id=proposed.id)


def old_vehicle(c, world):
    vid = str(uuid4())
    at = to_iso(utc_now())
    c.execute("""INSERT INTO vehicles(
        id,world_id,name,movement_type,speed_kmh,owner_character_id,
        is_available,metadata_json,created_at,updated_at
    ) VALUES (?,?,?,'land',12,?,1,'{"legacy":"原样保留"}',?,?)""",
        (vid, world["world_id"], "河湾双座马车", world["actor_id"], at, at))
    return vid


def test_vehicle_requires_explicit_confirmation_and_is_idempotent(database, vehicle_world):
    world = vehicle_world
    registry = WorldElementRegistry()
    request = request_for(world)
    with database.write() as c:
        before = c.execute("SELECT version FROM worlds WHERE id=?", (world["world_id"],)).fetchone()[0]
        actors_before = [tuple(row) for row in c.execute(
            "SELECT id,longitude,latitude,money,active_vehicle_id FROM characters WHERE world_id=? ORDER BY id",
            (world["world_id"],))]
        proposed = registry.submit(c, world_id=world["world_id"], request=request)
        assert proposed.status == "proposed"
        assert c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM vehicle_states").fetchone()[0] == 0
        assert c.execute("SELECT version FROM worlds WHERE id=?", (world["world_id"],)).fetchone()[0] == before
        result = registry.confirm(c, world_id=world["world_id"], registration_id=proposed.id)
        assert result.status == "applied"
        state = c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?", (result.result_entity_id,)).fetchone()
        assert state["capacity"] == 2
        assert state["longitude"] == world["location"]["longitude"]
        assert state["latitude"] == world["location"]["latitude"]
        assert state["space_layer"] == world["location"]["space_layer"]
        assert state["current_location_id"] == world["location"]["id"]
        assert state["status"] == "parked"
        assert state["registration_id"] == result.id
        assert state["distance_travelled_km"] == 0
        assert c.execute("SELECT version FROM worlds WHERE id=?", (world["world_id"],)).fetchone()[0] == before + 1
        assert registry.submit(c, world_id=world["world_id"], request=request).id == result.id
        with pytest.raises(RegistrationConflict):
            registry.confirm(c, world_id=world["world_id"], registration_id=proposed.id)
        assert c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
        assert actors_before == [tuple(row) for row in c.execute(
            "SELECT id,longitude,latitude,money,active_vehicle_id FROM characters WHERE world_id=? ORDER BY id",
            (world["world_id"],))]
        assert result.effects[0].payload == {"capacity": 2, "location_id": world["location"]["id"],
                                             "legacy_backfill": False}


def test_character_cannot_confirm_a_vehicle_into_existence(database, vehicle_world):
    world = vehicle_world
    request = request_for(world).model_copy(update={"requested_by_character_id": world["actor_id"]})
    registry = WorldElementRegistry()
    with database.write() as c:
        proposed = registry.submit(c, world_id=world["world_id"], request=request)
        result = registry.confirm(c, world_id=world["world_id"], registration_id=proposed.id)
        assert result.status == "rejected"
        assert "编年者" in result.rejection_reason
        assert c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0


@pytest.mark.parametrize("capacity", [0, -1, 65, True, 2.5, "2"])
def test_capacity_is_a_strict_positive_count_including_driver(capacity):
    with pytest.raises(ValidationError):
        VehicleSpec(name="双座车", movement_type="land", speed_kmh=12,
                    capacity=capacity, location_id="place")


@pytest.mark.parametrize("change", [{"speed_kmh": 0}, {"speed_kmh": float("inf")},
                                     {"fuel": 100}, {"longitude": 45}, {"space_layer": "sky"}])
def test_vehicle_payload_cannot_add_resources_or_invent_coordinates(change):
    payload = dict(name="双座车", movement_type="land", speed_kmh=12, capacity=2, location_id="place")
    payload.update(change)
    with pytest.raises(ValidationError):
        VehicleSpec.model_validate(payload)


@pytest.mark.parametrize("corruption", ["wrong_location", "future", "inactive", "missing_coordinates", "other_world"])
def test_unverified_location_or_source_does_not_create_partial_vehicle(database, vehicle_world, corruption):
    world = vehicle_world
    with database.write() as c:
        if corruption == "wrong_location":
            c.execute("UPDATE world_events SET location_id=NULL WHERE id=?", (world["source_id"],))
        elif corruption == "future":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?",
                      (to_iso(world["at"] + timedelta(seconds=1)), world["source_id"]))
        elif corruption == "inactive":
            c.execute("UPDATE locations SET is_active=0 WHERE id=?", (world["location"]["id"],))
        elif corruption == "missing_coordinates":
            c.execute("UPDATE locations SET longitude=NULL WHERE id=?", (world["location"]["id"],))
        else:
            other = WorldRepository().create_world(c, name="别处的世界", minutes_per_tick=60, seed_demo=True)
            world = dict(world, location=dict(c.execute(
                "SELECT * FROM locations WHERE world_id=? LIMIT 1", (other,)).fetchone()))
        result = register(c, world)
        assert result.status == "rejected"
        assert c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM vehicle_states").fetchone()[0] == 0


def test_vehicle_owner_must_belong_to_same_world(database, vehicle_world):
    with database.write() as c:
        result = register(c, vehicle_world, owner_character_id=str(uuid4()))
        assert result.status == "rejected"
        assert "所有者" in result.rejection_reason
        assert c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0


def test_legacy_backfill_preserves_original_vehicle_and_does_not_follow_owner(database, vehicle_world):
    world = vehicle_world
    with database.write() as c:
        vid = old_vehicle(c, world)
        before = tuple(c.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone())
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (world["actor_id"],))
        assert not c.execute("SELECT 1 FROM vehicle_states WHERE vehicle_id=?", (vid,)).fetchone()
        result = register(c, world, vehicle_id=vid)
        assert result.status == "applied"
        assert result.result_entity_id == vid
        assert before == tuple(c.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone())
        state = c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?", (vid,)).fetchone()
        assert state["longitude"] == world["location"]["longitude"]
        assert result.effects[0].payload["legacy_backfill"] is True


@pytest.mark.parametrize("change", [{"name": "偷偷改名"}, {"speed_kmh": 99},
                                     {"movement_type": "flight"}, {"owner_character_id": None}])
def test_legacy_backfill_cannot_rewrite_existing_vehicle_properties(database, vehicle_world, change):
    with database.write() as c:
        vid = old_vehicle(c, vehicle_world)
        result = register(c, vehicle_world, vehicle_id=vid, **change)
        assert result.status == "rejected"
        assert not c.execute("SELECT 1 FROM vehicle_states WHERE vehicle_id=?", (vid,)).fetchone()


def test_existing_state_cannot_be_relocated_or_expanded_by_another_registration(database, vehicle_world):
    with database.write() as c:
        first = register(c, vehicle_world)
        before = tuple(c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?",
                                 (first.result_entity_id,)).fetchone())
        second = register(c, vehicle_world, vehicle_id=first.result_entity_id, capacity=8)
        assert second.status == "rejected"
        assert "扩座" in second.rejection_reason
        assert before == tuple(c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?",
                                         (first.result_entity_id,)).fetchone())


def test_legacy_vehicle_still_selected_by_a_person_cannot_be_parked_by_registration(database, vehicle_world):
    with database.write() as c:
        vid = old_vehicle(c, vehicle_world)
        c.execute("UPDATE characters SET active_vehicle_id=? WHERE id=?", (vid, vehicle_world["actor_id"]))
        result = register(c, vehicle_world, vehicle_id=vid)
        assert result.status == "rejected"
        assert "正在使用" in result.rejection_reason
        assert not c.execute("SELECT 1 FROM vehicle_states WHERE vehicle_id=?", (vid,)).fetchone()


@pytest.mark.parametrize("movement_type,kind", [("ship", "port"), ("water", "dock"),
                                                ("flight", "airfield"), ("underground", "underground_station")])
def test_special_vehicle_requires_a_registered_suitable_stop(database, vehicle_world, movement_type, kind):
    with database.write() as c:
        rejected = register(c, vehicle_world, movement_type=movement_type)
        assert rejected.status == "rejected"
        assert "停靠设施" in rejected.rejection_reason
        c.execute("UPDATE locations SET kind=? WHERE id=?", (kind, vehicle_world["location"]["id"]))
        accepted = register(c, vehicle_world, movement_type=movement_type)
        assert accepted.status == "applied"


def test_source_is_checked_again_at_confirmation(database, vehicle_world):
    registry = WorldElementRegistry()
    with database.write() as c:
        draft = registry.submit(c, world_id=vehicle_world["world_id"], request=request_for(vehicle_world))
        c.execute("UPDATE locations SET is_active=0 WHERE id=?", (vehicle_world["location"]["id"],))
        result = registry.confirm(c, world_id=vehicle_world["world_id"], registration_id=draft.id)
        assert result.status == "rejected"
        assert c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0


def legacy_registration_connection(factory=sqlite3.Connection):
    c = sqlite3.connect(":memory:", factory=factory)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA.replace("'npc_routine','vehicle'", "'npc_routine'"))
    c.execute("INSERT INTO worlds(id,name,current_time,minutes_per_tick,created_at,updated_at) "
              "VALUES ('world','旧世界','2000-01-01T00:00:00+00:00',60,'a','b')")
    c.execute("INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,summary,created_at) "
              "VALUES ('event','world','tick','2000-01-01T00:00:00+00:00','verified','原事件','c')")
    c.execute("""INSERT INTO element_registration_requests(
        id,world_id,element_type,source_event_id,idempotency_key,status,payload_json,
        input_world_version,applied_world_version,result_entity_id,created_at,updated_at
    ) VALUES ('registration','world','lore','event','legacy:register','applied',
        '{"title":"旧世界原始知识","content":"保留空格  与内容"}',1,2,'knowledge','d','e')""")
    c.execute("INSERT INTO world_entities(id,world_id,entity_type,entity_id,name,registration_id,created_at) "
              "VALUES ('entity','world','knowledge','knowledge','旧知识','registration','f')")
    c.execute("""INSERT INTO element_registration_effects(
        id,registration_id,world_id,effect_type,payload_json,created_at
    ) VALUES ('effect','registration','world','lore.registered','{"kept":true}','g')""")
    c.execute("CREATE INDEX idx_legacy_request_payload ON element_registration_requests(payload_json)")
    c.execute("CREATE TABLE migration_audit (registration_id TEXT)")
    c.execute("CREATE TRIGGER legacy_request_audit AFTER UPDATE OF status ON element_registration_requests "
              "BEGIN INSERT INTO migration_audit(registration_id) VALUES (new.id); END")
    c.commit()
    return c


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_vehicle_type_migration_preserves_old_rows_references_indexes_and_triggers(foreign_keys):
    c = legacy_registration_connection()
    try:
        c.execute(f"PRAGMA foreign_keys={foreign_keys}")
        tables = ("element_registration_requests", "world_entities", "element_registration_effects", "world_events")
        before = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table}")] for table in tables}
        migrations.migrate_registration_element_types(c)
        assert before == {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table}")] for table in tables}
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='idx_legacy_request_payload'").fetchone()
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='legacy_request_audit'").fetchone()
        assert c.execute("SELECT COUNT(*) FROM migration_audit").fetchone()[0] == 0
        version = c.execute("PRAGMA schema_version").fetchone()[0]
        migrations.migrate_registration_element_types(c)
        assert c.execute("PRAGMA schema_version").fetchone()[0] == version
        c.execute("UPDATE element_registration_requests SET status='applied' WHERE id='registration'")
        assert c.execute("SELECT registration_id FROM migration_audit").fetchone()[0] == "registration"
        c.execute("""INSERT INTO element_registration_requests(
            id,world_id,element_type,source_event_id,idempotency_key,status,payload_json,
            input_world_version,created_at,updated_at
        ) VALUES ('vehicle','world','vehicle','event','vehicle:new','proposed','{}',2,'h','i')""")
    finally:
        c.close()


def test_registration_type_migration_rolls_back_all_ddl_on_failure():
    class RenameFailureConnection(sqlite3.Connection):
        fail_rename = False

        def execute(self, sql, parameters=()):
            if self.fail_rename and sql.startswith("ALTER TABLE element_registration_requests_next"):
                raise RuntimeError("模拟载具登记迁移的重命名失败")
            return super().execute(sql, parameters)

    c = legacy_registration_connection(RenameFailureConnection)
    try:
        original = c.execute("SELECT sql FROM sqlite_master WHERE name='element_registration_requests'").fetchone()[0]
        before = tuple(c.execute("SELECT * FROM element_registration_requests").fetchone())
        c.fail_rename = True
        with pytest.raises(RuntimeError, match="重命名失败"):
            migrations.migrate_registration_element_types(c)
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='element_registration_requests'").fetchone()[0] == original
        assert tuple(c.execute("SELECT * FROM element_registration_requests").fetchone()) == before
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='element_registration_requests_next'").fetchone()
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        c.close()
