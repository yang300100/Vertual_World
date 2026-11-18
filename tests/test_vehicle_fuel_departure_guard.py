"""哥哥，确认规格及装载不会绕过尚未实现的真实行驶消耗。"""

import pytest

from test_rides import ride_world, current  # noqa: F401
from test_vehicle_fuel_profile import fuel_world, apply  # noqa: F401
from world_engine.movement import MovementService
from world_engine.player_steering import PlayerSteeringService
from world_engine.repository import utc_now
from world_engine.rides import RideService


def test_confirmed_profile_blocks_all_new_departure_paths(fuel_world):
    e = fuel_world
    with e.db.write() as c:
        assert apply(c, e).status == "applied"
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        before = dict(actor)
        with pytest.raises(ValueError, match="燃料|载具"):
            MovementService().select_transport(c, world_id=e.wid, character_id=e.player, vehicle_id=e.vehicle)
        with pytest.raises(ValueError, match="燃料"):
            MovementService()._start_solo(c, world_id=e.wid, character_id=e.player, vehicle_id=e.vehicle,
                destination_longitude=e.loc["longitude"] + 0.01, destination_latitude=e.loc["latitude"], world_time=e.at)
        vehicle = c.execute("SELECT * FROM vehicles WHERE id=?", (e.vehicle,)).fetchone()
        with pytest.raises(ValueError, match="燃料"):
            RideService._driver_state(c, {"world_id": e.wid}, e.at, e.player, vehicle)
        assert dict(c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()) == before
        assert not c.execute("SELECT 1 FROM character_movements WHERE vehicle_id=?", (e.vehicle,)).fetchone()


def test_existing_selected_profile_cannot_start_new_direction(fuel_world):
    e = fuel_world
    with e.db.write() as c:
        assert apply(c, e).status == "applied"
        # 哥哥，模拟不一致存档以验证入口本身仍拒绝，不在正式世界设置这个状态。
        c.execute("UPDATE characters SET active_vehicle_id=? WHERE id=?", (e.vehicle, e.player))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        before = dict(actor)
        with pytest.raises(ValueError, match="燃料|载具"):
            PlayerSteeringService.set_direction(c, actor, (1, 0), 1, current(e, c), utc_now())
        assert dict(c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()) == before
        assert not PlayerSteeringService.active(c, e.player)
