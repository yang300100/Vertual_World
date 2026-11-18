"""哥哥，只用隔离世界检验燃料规格与原登记事实的时序。"""

from datetime import timedelta

import pytest

from test_rides import ride_world  # noqa: F401
from test_vehicle_fuel_profile import fuel_world  # noqa: F401
from test_vehicle_fuel_inventory import fuel_inventory, action, snapshot  # noqa: F401
from world_engine.repository import to_iso
from world_engine.vehicle_fuel_inventory import VehicleFuelInventoryService as Fuel


@pytest.mark.parametrize("late_registration", ["vehicle", "commodity"])
def test_later_original_confirmation_cannot_retroactively_validate_fuel(fuel_inventory, late_registration):
    e = fuel_inventory
    with e.db.write() as c:
        if late_registration == "vehicle":
            reg = c.execute("SELECT registration_id FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
        else:
            reg = c.execute("SELECT registration_id FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (e.wid, e.item_type)).fetchone()[0]
        # 哥哥，确认事件已在当前时刻发生，但仍晚于规格成立所需的历史事实。
        now = e.at + timedelta(hours=1)
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(now), e.wid))
        c.execute("UPDATE world_events SET occurred_at=? WHERE tick_id=? AND event_type='world.element_registered'", (to_iso(e.at + timedelta(minutes=1)), reg))
        before = snapshot(c, e)
        with pytest.raises(ValueError):
            action(c, e, at=now)
        assert Fuel.options(c, world_id=e.wid, actor_id=e.player, at=now) == []
        assert snapshot(c, e) == before


def test_simultaneous_confirmations_remain_valid_after_later_move(fuel_inventory):
    e = fuel_inventory
    with e.db.write() as c:
        now = e.at + timedelta(hours=1)
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(now), e.wid))
        destination = c.execute("SELECT * FROM locations WHERE id=?", (e.destination,)).fetchone()
        # 哥哥，地点是已到达后的隔离初态；旧登记来源仍留在实际发生地点。
        c.execute("UPDATE vehicle_states SET longitude=?,latitude=?,current_location_id=? WHERE vehicle_id=?", (destination["longitude"], destination["latitude"], e.destination, e.vehicle))
        c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?", (destination["longitude"], destination["latitude"], e.destination, e.player))
        result = action(c, e, at=now)
        assert result["status"] == "applied"
        assert c.execute("SELECT location_id FROM world_events WHERE id=?", (result["event_id"],)).fetchone()[0] == e.destination


@pytest.mark.parametrize("late_registration", ["vehicle", "commodity", "profile_source"])
def test_simultaneous_events_still_respect_recorded_event_order(fuel_inventory, late_registration):
    e = fuel_inventory
    with e.db.write() as c:
        if late_registration == "vehicle":
            reg = c.execute("SELECT registration_id FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0]
            event = c.execute("SELECT id FROM world_events WHERE tick_id=? AND event_type='world.element_registered'", (reg,)).fetchone()[0]
        elif late_registration == "commodity":
            reg = c.execute("SELECT registration_id FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (e.wid, e.item_type)).fetchone()[0]
            event = c.execute("SELECT id FROM world_events WHERE tick_id=? AND event_type='world.element_registered'", (reg,)).fetchone()[0]
        else:
            event = e.source
        # 哥哥，只调整隔离事件的真实记录顺序，保持其世界时刻与其他事件相同。
        last_order = c.execute("SELECT MAX(rowid) FROM world_events").fetchone()[0]
        c.execute("UPDATE world_events SET rowid=? WHERE id=?", (last_order + 1, event))
        before = snapshot(c, e)
        with pytest.raises(ValueError):
            action(c, e)
        assert Fuel.options(c, world_id=e.wid, actor_id=e.player, at=e.at) == []
        assert snapshot(c, e) == before
