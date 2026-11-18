"""哥哥，燃料装卸只在独立存档中验证实物、物权与原子回执。"""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from test_rides import ride_world  # noqa: F401
from test_ride_runtime import rental
from test_vehicle_fuel_profile import fuel_world, apply  # noqa: F401
from world_engine.actions import ActionService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.removal import ElementRemovalSubmit, WorldElementRemover
from world_engine.repository import to_iso
from world_engine.vehicle_fuel_inventory import VehicleFuelInventoryService as Fuel


@pytest.fixture
def fuel_inventory(fuel_world):
    e = fuel_world
    with e.db.write() as c:
        assert apply(c, e).status == "applied"
        c.execute("UPDATE characters SET inventory_capacity=20 WHERE id IN (?,?)", (e.player, e.npc))
        e.item = make_fuel(c, e, e.player, 10)
    return e


def make_fuel(c, e, owner, quantity, *, carrier="fuel", tracked=True, item_type=None):
    iid = str(uuid4())
    c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) VALUES (?,?,?,'character_inventory',?,?,100,?)",
              (iid, e.wid, item_type or e.item_type, owner, quantity, owner))
    if tracked:
        MaterialService.attach(c, iid, MaterialState(carrier=carrier, mass=quantity * 2))
    return iid


def action(c, e, operation="load", *, actor=None, item=None, quantity=3, key="one", at=None):
    return getattr(Fuel, operation)(c, world_id=e.wid, actor_id=actor or e.player,
        vehicle_id=e.vehicle, item_id=item or e.item, quantity=quantity, idempotency_key=key, at=at or e.at)


def snapshot(c, e):
    tables = ("item_instances", "material_batches", "material_ledger", "material_sinks", "inventory_changes", "vehicle_fuel_inventory_receipts", "world_events")
    return {t: [tuple(x) for x in c.execute("SELECT * FROM " + t + " ORDER BY rowid")] for t in tables} | {
        "version": c.execute("SELECT version FROM worlds WHERE id=?", (e.wid,)).fetchone()[0]}


def test_load_unload_preserves_material_and_original_receipt_after_batch_moves(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        result = action(c, e)
        assert result["status"] == "applied"
        moved = result["moved_item_id"]
        assert c.execute("SELECT quantity,owner_character_id,container_type,container_id FROM item_instances WHERE id=?", (moved,)).fetchone()[:] == (3, e.player, "vehicle_fuel", e.vehicle)
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (e.item,)).fetchone()[0] == 7
        assert MaterialService.state(c, moved).mass == 6 and MaterialService.state(c, e.item).mass == 14
        event = c.execute("SELECT location_id,actor_id FROM world_events WHERE id=?", (result["event_id"],)).fetchone()
        assert tuple(event) == (e.origin, e.player)
        assert c.execute("SELECT COUNT(*) FROM inventory_changes WHERE source_event_id=?", (result["event_id"],)).fetchone()[0] == 2
        second = action(c, e, "unload", item=moved, quantity=3, key="two")
        assert c.execute("SELECT container_type,container_id,owner_character_id FROM item_instances WHERE id=?", (moved,)).fetchone()[:] == ("character_inventory", e.player, e.player)
        before = snapshot(c, e)
        repeat = action(c, e)
        assert repeat["status"] == "already" and repeat["event_id"] == result["event_id"] and repeat["moved_item_id"] == moved
        assert snapshot(c, e) == before
        assert action(c, e, "unload", item=moved, quantity=3, key="two")["event_id"] == second["event_id"]
        assert snapshot(c, e) == before
        with pytest.raises(ValueError, match="幂等键"):
            action(c, e, quantity=2)
        assert snapshot(c, e) == before


@pytest.mark.parametrize("fault", ["zero", "bool", "fraction", "foreign_owner", "untracked", "wrong_carrier", "wrong_item", "remote", "layer", "health", "moving_vehicle", "future_time", "over_capacity"])
def test_invalid_fuel_authority_location_and_quantity_are_atomic(fuel_inventory, fault):
    e = fuel_inventory
    with e.db.write() as c:
        iid, qty, at = e.item, 3, e.at
        if fault == "zero": qty = 0
        elif fault == "bool": qty = True
        elif fault == "fraction": qty = 1.5
        elif fault == "foreign_owner": iid = make_fuel(c, e, e.npc, 5)
        elif fault == "untracked": iid = make_fuel(c, e, e.player, 5, tracked=False)
        elif fault == "wrong_carrier": iid = make_fuel(c, e, e.player, 5, carrier="grain")
        elif fault == "wrong_item": iid = make_fuel(c, e, e.player, 5, item_type=c.execute("SELECT id FROM item_types WHERE id<>? LIMIT 1", (e.item_type,)).fetchone()[0])
        elif fault == "remote": c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.player,))
        elif fault == "layer": c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (e.player,))
        elif fault == "health": c.execute("UPDATE characters SET health=0 WHERE id=?", (e.player,))
        elif fault == "moving_vehicle": c.execute("UPDATE vehicle_states SET status='moving' WHERE vehicle_id=?", (e.vehicle,))
        elif fault == "future_time": at = e.at + timedelta(seconds=1)
        elif fault == "over_capacity": iid = make_fuel(c, e, e.player, 15); qty = 13
        before = snapshot(c, e)
        with pytest.raises(ValueError): action(c, e, item=iid, quantity=qty, at=at)
        assert snapshot(c, e) == before


def test_unload_preserves_each_owners_rights_and_options_are_private(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        first = action(c, e, quantity=4)
        foreign_id = make_fuel(c, e, e.npc, 3)
        foreign = c.execute("SELECT * FROM item_instances WHERE id=?", (foreign_id,)).fetchone()
        MaterialService.move(c, foreign, 3, "vehicle_fuel", e.vehicle, e.npc)
        before = c.total_changes
        choices = Fuel.options(c, world_id=e.wid, actor_id=e.player, at=e.at)
        assert c.total_changes == before and len(choices) == 1
        assert choices[0]["loaded_units"] == 7
        assert [row["id"] for row in choices[0]["unloadable"]] == [first["moved_item_id"]]
        assert foreign_id not in str(choices)
        with pytest.raises(ValueError): action(c, e, "unload", item=foreign_id, quantity=1, key="foreign")
        assert c.execute("SELECT owner_character_id,container_type FROM item_instances WHERE id=?", (foreign_id,)).fetchone()[:] == (e.npc, "vehicle_fuel")
        assert Fuel.options(c, world_id=e.wid, actor_id=e.npc, at=e.at) == []


def test_removal_refuses_stored_fuel_then_allows_empty_vehicle(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        loaded = action(c, e, quantity=3)
        def remove(key):
            return WorldElementRemover().submit(c, world_id=e.wid, request=ElementRemovalSubmit(
                requested_by_character_id=e.player, source_event_id=e.source, idempotency_key=key,
                target_element_type="vehicle", target_entity_id=e.vehicle, reason="destroyed", details="隔离移除验收。"))
        rejected = remove("fuel-removal-with-fuel")
        assert rejected.status == "rejected" and "燃料" in rejected.rejection_reason
        assert c.execute("SELECT is_available FROM vehicles WHERE id=?", (e.vehicle,)).fetchone()[0] == 1
        action(c, e, "unload", item=loaded["moved_item_id"], quantity=3, key="unload-for-removal")
        assert remove("fuel-removal-empty").status == "applied"


def test_each_person_can_retrieve_only_their_own_fuel_when_authorized(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        first = action(c, e, quantity=4)
        foreign_id = make_fuel(c, e, e.npc, 3)
        # 哥哥，隔离初态模拟车权合法变化，不由装卸服务转移车辆所有权。
        c.execute("UPDATE vehicles SET owner_character_id=? WHERE id=?", (e.npc, e.vehicle))
        npc_load = action(c, e, actor=e.npc, item=foreign_id, quantity=3, key="npc-load")
        with pytest.raises(ValueError):
            action(c, e, "unload", actor=e.npc, item=first["moved_item_id"], quantity=1, key="npc-take-other")
        npc_unload = action(c, e, "unload", actor=e.npc, item=npc_load["moved_item_id"], quantity=3, key="npc-unload")
        assert c.execute("SELECT owner_character_id,container_id FROM item_instances WHERE id=?", (npc_unload["moved_item_id"],)).fetchone()[:] == (e.npc, e.npc)
        with pytest.raises(ValueError):
            action(c, e, "unload", item=first["moved_item_id"], quantity=4, key="no-current-right")
        c.execute("UPDATE vehicles SET owner_character_id=? WHERE id=?", (e.player, e.vehicle))
        player_unload = action(c, e, "unload", item=first["moved_item_id"], quantity=4, key="player-unload")
        assert c.execute("SELECT owner_character_id,container_id FROM item_instances WHERE id=?", (player_unload["moved_item_id"],)).fetchone()[:] == (e.player, e.player)


@pytest.mark.parametrize("failure", ["event", "inventory_audit"])
def test_event_or_inventory_failure_rolls_back_batch_and_receipt(fuel_inventory, monkeypatch, failure):
    e = fuel_inventory
    with e.db.write() as c:
        before = snapshot(c, e)
        def fail(*args, **kwargs):
            raise RuntimeError("隔离验收：注入后续事务失败")
        monkeypatch.setattr(ActionService if failure == "event" else __import__("world_engine.inventory", fromlist=["InventoryService"]).InventoryService,
                            "_record_event" if failure == "event" else "audit", fail)
        with pytest.raises(RuntimeError):
            action(c, e)
        assert snapshot(c, e) == before


def test_missing_original_event_never_replays_fuel_move(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        result = action(c, e)
        c.execute("DELETE FROM world_events WHERE id=?", (result["event_id"],))
        before = snapshot(c, e)
        with pytest.raises(ValueError, match="事件已不存在"):
            action(c, e)
        assert snapshot(c, e) == before


def test_real_paid_lessee_can_load_and_owner_cannot_take_lessee_fuel(fuel_world):
    e = fuel_world
    rental(e, seconds=3600)
    with e.db.write() as c:
        e.source = ActionService._record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="action.registration_source", actor_id=e.player, target_id=None, location_id=e.origin,
            summary="隔离场景：核验租用车辆的规格。", payload={"vehicle_id": e.vehicle})
        assert apply(c, e).status == "applied"
        c.execute("UPDATE characters SET inventory_capacity=20 WHERE id IN (?,?)", (e.player, e.npc))
        e.item = make_fuel(c, e, e.player, 5)
        loaded = action(c, e, quantity=3)
        assert Fuel.options(c, world_id=e.wid, actor_id=e.npc, at=e.at) == []
        with pytest.raises(ValueError):
            action(c, e, "unload", actor=e.npc, item=loaded["moved_item_id"], quantity=1, key="owner-take")
        unloaded = action(c, e, "unload", item=loaded["moved_item_id"], quantity=3, key="lessee-unload")
        assert c.execute("SELECT owner_character_id,container_id FROM item_instances WHERE id=?", (unloaded["moved_item_id"],)).fetchone()[:] == (e.player, e.player)


@pytest.mark.parametrize("fault", ["profile_future", "profile_deleted", "profile_wrong_source", "profile_wrong_registration", "profile_wrong_effect", "item_future", "item_deleted", "item_wrong_entity"])
def test_confirmation_events_and_effects_are_required_for_real_transfer(fuel_inventory, fault):
    e = fuel_inventory
    with e.db.write() as c:
        profile_id = c.execute("SELECT registration_id FROM vehicle_fuel_profiles WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
        item_reg = c.execute("SELECT registration_id FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (e.wid, e.item_type)).fetchone()[0]
        reg = item_reg if fault.startswith("item_") else profile_id
        event = c.execute("SELECT * FROM world_events WHERE tick_id=? AND event_type='world.element_registered'", (reg,)).fetchone()
        if fault.endswith("future"):
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(minutes=1)), event["id"]))
        elif fault.endswith("deleted"):
            c.execute("DELETE FROM world_events WHERE id=?", (event["id"],))
        elif fault == "profile_wrong_effect":
            c.execute("UPDATE element_registration_effects SET payload_json='{}' WHERE registration_id=?", (reg,))
        else:
            payload = json.loads(event["payload_json"])
            payload[{"profile_wrong_source": "source_event_id", "profile_wrong_registration": "registration_id", "item_wrong_entity": "entity_id"}[fault]] = str(uuid4())
            c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (json.dumps(payload), event["id"]))
        before = snapshot(c, e)
        with pytest.raises(ValueError):
            action(c, e)
        assert Fuel.options(c, world_id=e.wid, actor_id=e.player, at=e.at) == []
        assert snapshot(c, e) == before


def test_new_location_does_not_rewrite_original_registration_location(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        destination = c.execute("SELECT * FROM locations WHERE id=?", (e.destination,)).fetchone()
        # 哥哥，隔离场景预置车辆和本人已到新地点，旧登记与确认来源仍留在原地点。
        c.execute("UPDATE vehicle_states SET longitude=?,latitude=?,current_location_id=? WHERE vehicle_id=?",
                  (destination["longitude"], destination["latitude"], e.destination, e.vehicle))
        c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
                  (destination["longitude"], destination["latitude"], e.destination, e.player))
        loaded = action(c, e)
        assert c.execute("SELECT location_id FROM world_events WHERE id=?", (loaded["event_id"],)).fetchone()[0] == e.destination
        assert c.execute("SELECT location_id FROM world_events WHERE id=?", (e.source,)).fetchone()[0] == e.origin


@pytest.mark.parametrize("fault", ["vehicle_effect", "vehicle_source_location", "vehicle_event_entity"])
def test_vehicle_physical_registration_source_cannot_be_tampered(fuel_inventory, fault):
    e = fuel_inventory
    with e.db.write() as c:
        reg = c.execute("SELECT registration_id FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
        row = c.execute("SELECT source_event_id FROM element_registration_requests WHERE id=?", (reg,)).fetchone()
        event = c.execute("SELECT * FROM world_events WHERE tick_id=? AND event_type='world.element_registered'", (reg,)).fetchone()
        if fault == "vehicle_effect":
            c.execute("UPDATE element_registration_effects SET payload_json='{}' WHERE registration_id=?", (reg,))
        elif fault == "vehicle_source_location":
            c.execute("UPDATE world_events SET location_id=NULL WHERE id=?", (row["source_event_id"],))
        else:
            payload = json.loads(event["payload_json"])
            payload["entity_id"] = str(uuid4())
            c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (json.dumps(payload), event["id"]))
        before = snapshot(c, e)
        with pytest.raises(ValueError): action(c, e)
        assert snapshot(c, e) == before


@pytest.mark.parametrize("fault", ["vehicle_confirmation_missing", "vehicle_confirmation_future", "source_vehicle_changed", "source_location_changed"])
def test_vehicle_and_profile_origins_require_actual_confirmed_sources(fuel_inventory, fault):
    e = fuel_inventory
    with e.db.write() as c:
        if fault.startswith("vehicle_confirmation"):
            reg = c.execute("SELECT registration_id FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
            event = c.execute("SELECT id FROM world_events WHERE world_id=? AND event_type='world.element_registered' AND tick_id=?", (e.wid, reg)).fetchone()
            assert event is not None
            if fault.endswith("missing"):
                c.execute("DELETE FROM world_events WHERE id=?", (event[0],))
            else:
                c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(hours=1)), event[0]))
        elif fault == "source_vehicle_changed":
            c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (e.source,))
        else:
            c.execute("UPDATE world_events SET location_id=NULL WHERE id=?", (e.source,))
        before = snapshot(c, e)
        with pytest.raises(ValueError): action(c, e)
        assert snapshot(c, e) == before
