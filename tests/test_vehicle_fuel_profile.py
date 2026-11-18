"""哥哥，规格登记只在隔离存档验证，不能创建或覆写燃料。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from test_rides import ride_world, boarded, choose, current  # noqa: F401
from world_engine.actions import ActionService
from world_engine.registration import (
    ElementRegistrationSubmit, RegistrationConflict, VehicleFuelProfileSpec, WorldElementRegistry,
)
from world_engine.repository import to_iso


@pytest.fixture
def fuel_world(ride_world):
    e = ride_world
    with e.db.write() as c:
        source = ActionService._record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=current(e, c),
            event_type="action.registration_source", actor_id=e.player, target_id=None, location_id=e.origin,
            summary="隔离场景：核验已存在的车与燃料类型。", payload={"vehicle_id": e.vehicle})
        commodity = WorldElementRegistry().submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
            "source_event_id": source, "idempotency_key": "fuel-item:" + uuid4().hex,
            "payload": {"element_type": "commodity", "name": "隔离燃料单位", "category": "material", "nutrition": 0},
        }))
        assert commodity.status == "applied"
        e.source = source
        e.item_type = commodity.result_entity_id
    return e


def request(e, **changes):
    payload = {"element_type": "vehicle_fuel_profile", "vehicle_id": e.vehicle, "item_type_id": e.item_type,
               "capacity_units": 12, "rate_numerator": 3, "rate_denominator": 10}
    payload.update(changes)
    return ElementRegistrationSubmit.model_validate({"source_event_id": e.source,
        "idempotency_key": "fuel-profile:" + uuid4().hex, "payload": payload})


def apply(c, e, req=None):
    registry = WorldElementRegistry()
    proposal = registry.submit(c, world_id=e.wid, request=req or request(e))
    assert proposal.status == "proposed"
    return registry.confirm(c, world_id=e.wid, registration_id=proposal.id)


def test_profile_requires_confirmation_does_not_create_fuel_or_change_vehicle_provenance(fuel_world):
    e = fuel_world
    req = request(e)
    with e.db.write() as c:
        old_vehicle = dict(c.execute("SELECT * FROM vehicles WHERE id=?", (e.vehicle,)).fetchone())
        old_state = dict(c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone())
        old_entity = dict(c.execute("SELECT * FROM world_entities WHERE entity_id=? AND entity_type='vehicle'", (e.vehicle,)).fetchone())
        old_catalog = dict(c.execute("SELECT * FROM world_element_catalog WHERE entity_id=? AND entity_type='vehicle'", (e.vehicle,)).fetchone())
        before = [tuple(row) for row in c.execute("SELECT * FROM item_instances WHERE world_id=? ORDER BY id", (e.wid,))]
        version = c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0]
        registry = WorldElementRegistry()
        proposal = registry.submit(c, world_id=e.wid, request=req)
        assert proposal.status == "proposed" and not c.execute("SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()
        assert registry.submit(c, world_id=e.wid, request=req).id == proposal.id
        result = registry.confirm(c, world_id=e.wid, registration_id=proposal.id)
        assert result.status == "applied" and result.result_entity_id is None
        profile = dict(c.execute("SELECT * FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone())
        assert profile == {"vehicle_id": e.vehicle, "world_id": e.wid, "item_type_id": e.item_type,
                           "capacity_units": 12, "rate_numerator": 3, "rate_denominator": 10, "registration_id": proposal.id}
        assert result.effects[0].entity_id == e.vehicle
        assert registry.submit(c, world_id=e.wid, request=req).id == result.id
        with pytest.raises(RegistrationConflict):
            registry.confirm(c, world_id=e.wid, registration_id=proposal.id)
        assert dict(c.execute("SELECT * FROM vehicles WHERE id=?", (e.vehicle,)).fetchone()) == old_vehicle
        assert dict(c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()) == old_state
        assert dict(c.execute("SELECT * FROM world_entities WHERE entity_id=? AND entity_type='vehicle'", (e.vehicle,)).fetchone()) == old_entity
        assert dict(c.execute("SELECT * FROM world_element_catalog WHERE entity_id=? AND entity_type='vehicle'", (e.vehicle,)).fetchone()) == old_catalog
        assert [tuple(row) for row in c.execute("SELECT * FROM item_instances WHERE world_id=? ORDER BY id", (e.wid,))] == before
        assert c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0] == version + 1
        assert apply(c, e, request(e, capacity_units=24)).status == "rejected"
        assert dict(c.execute("SELECT * FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()) == profile


@pytest.mark.parametrize("field", ["capacity_units", "rate_numerator", "rate_denominator"])
@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2", 1000000001])
def test_profile_integers_are_strict_and_bounded(field, bad):
    data = {"vehicle_id": "vehicle", "item_type_id": "fuel", "capacity_units": 10, "rate_numerator": 1, "rate_denominator": 10}
    data[field] = bad
    with pytest.raises(ValidationError):
        VehicleFuelProfileSpec.model_validate(data)


@pytest.mark.parametrize("fault", ["future", "source_vehicle", "source_location", "source_json", "source_before_vehicle", "item_missing", "item_future", "source_actor", "active_vehicle", "wrong_position", "inactive_place", "foreign_vehicle"])
def test_invalid_source_state_or_authority_rejects_without_effect(fuel_world, fault):
    e = fuel_world
    with e.db.write() as c:
        req = request(e)
        if fault == "future":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(seconds=1)), e.source))
        elif fault == "source_vehicle":
            c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (e.source,))
        elif fault == "source_location":
            c.execute("UPDATE world_events SET location_id=NULL WHERE id=?", (e.source,))
        elif fault == "source_json":
            c.execute("UPDATE world_events SET payload_json='[]' WHERE id=?", (e.source,))
        elif fault == "source_before_vehicle":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at - timedelta(seconds=1)), e.source))
        elif fault == "item_missing":
            req = request(e, item_type_id=str(uuid4()))
        elif fault == "item_future":
            c.execute("UPDATE world_events SET occurred_at=? WHERE event_type='world.element_registered' "
                      "AND json_extract(payload_json,'$.entity_id')=?", (to_iso(e.at + timedelta(seconds=1)), e.item_type))
        elif fault == "source_actor":
            req = req.model_copy(update={"requested_by_character_id": e.player})
        elif fault == "active_vehicle":
            c.execute("UPDATE characters SET active_vehicle_id=? WHERE id=?", (e.vehicle, e.player))
        elif fault == "wrong_position":
            c.execute("UPDATE vehicle_states SET longitude=longitude+1 WHERE vehicle_id=?", (e.vehicle,))
        elif fault == "inactive_place":
            c.execute("UPDATE locations SET is_active=0 WHERE id=?", (e.origin,))
        elif fault == "foreign_vehicle":
            req = request(e, vehicle_id=str(uuid4()))
        old_vehicle = dict(c.execute("SELECT * FROM vehicles WHERE id=?", (e.vehicle,)).fetchone())
        result = apply(c, e, req)
        assert result.status == "rejected", result
        assert not c.execute("SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()
        assert dict(c.execute("SELECT * FROM vehicles WHERE id=?", (e.vehicle,)).fetchone()) == old_vehicle


def test_insert_failure_rolls_back_profile_and_world_version(fuel_world):
    e = fuel_world
    with e.db.write() as c:
        c.execute("CREATE TRIGGER test_reject_fuel_profile BEFORE INSERT ON vehicle_fuel_profiles "
                  "BEGIN SELECT RAISE(ABORT,'隔离故障'); END")
        before = c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0]
        result = apply(c, e)
        assert result.status == "failed"
        assert not c.execute("SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()
        assert c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0] == before


def test_ongoing_ride_cannot_be_retroactively_assigned_a_profile(fuel_world):
    e = fuel_world
    boarded(e)
    with e.db.write() as c:
        result = apply(c, e)
        assert result.status == "rejected"
        assert not c.execute("SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()


@pytest.mark.parametrize("status", ["moving", "suspended"])
def test_existing_route_even_suspended_cannot_be_reinterpreted(fuel_world, status):
    e = fuel_world
    rid = boarded(e)
    choose(e, e.player, "depart", rid)
    with e.db.write() as c:
        movement = c.execute("SELECT * FROM character_movements WHERE vehicle_id=?", (e.vehicle,)).fetchone()
        c.execute("UPDATE character_movements SET status=? WHERE id=?", (status, movement["id"]))
        c.execute("UPDATE vehicle_rides SET status='ended' WHERE id=?", (rid,))
        c.execute("UPDATE vehicle_ride_members SET disembarked_at=boarded_at WHERE ride_id=?", (rid,))
        c.execute("UPDATE vehicle_states SET status='parked' WHERE vehicle_id=?", (e.vehicle,))
        result = apply(c, e)
        assert result.status == "rejected" and "路线" in result.rejection_reason


def test_metadata_is_preserved_not_converted_to_fuel_balance(fuel_world):
    e = fuel_world
    metadata = {"fuel": 17, "fuel_description": "旧资料待核实"}
    with e.db.write() as c:
        c.execute("UPDATE vehicles SET metadata_json=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), e.vehicle))
        result = apply(c, e)
        assert result.status == "applied"
        assert json.loads(c.execute("SELECT metadata_json FROM vehicles WHERE id=?", (e.vehicle,)).fetchone()[0]) == metadata
        assert not c.execute("SELECT 1 FROM item_instances WHERE container_id=? AND container_type='vehicle_fuel'", (e.vehicle,)).fetchone()


@pytest.mark.parametrize("fault", ["vehicle_entity", "vehicle_source", "vehicle_effect", "vehicle_order", "commodity_entity", "commodity_source"])
def test_profile_confirmation_rechecks_original_registration_chain(fuel_world, fault):
    e = fuel_world
    with e.db.write() as c:
        if fault.startswith("vehicle_"):
            reg = c.execute("SELECT registration_id FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
        else:
            reg = c.execute("SELECT registration_id FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (e.wid, e.item_type)).fetchone()[0]
        event = c.execute("SELECT * FROM world_events WHERE world_id=? AND tick_id=? AND event_type='world.element_registered'", (e.wid, reg)).fetchone()
        assert event is not None
        if fault == "vehicle_effect":
            c.execute("UPDATE element_registration_effects SET payload_json='{}' WHERE registration_id=?", (reg,))
        elif fault == "vehicle_order":
            # 哥哥，同一世界时刻把确认事件放到规格来源之后，不能伪造先有车。
            order = c.execute("SELECT MAX(rowid)+1 FROM world_events").fetchone()[0]
            c.execute("UPDATE world_events SET rowid=? WHERE id=?", (order, event["id"]))
        else:
            payload = json.loads(event["payload_json"])
            payload["source_event_id" if fault.endswith("source") else "entity_id"] = str(uuid4())
            c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (json.dumps(payload), event["id"]))
        result = apply(c, e)
        assert result.status == "rejected", result
        assert not c.execute("SELECT 1 FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()
