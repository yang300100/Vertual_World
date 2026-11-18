"""哥哥，逾期的共乘在当前真实时刻停下，不能补写过去发生的事件。"""

from datetime import timedelta

import pytest

from test_navigation_corrupt_companionship import install_known_route, slow_real_routes
from test_rides import boarded, choose, ride_world  # noqa: F401
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso, utc_now


@pytest.mark.parametrize(
    "previous_minutes,current_minutes,expected_minutes,expect_progress",
    [(10, 11, 10, False), (0, 10, 5, True)],
    ids=["逾期存档从当前时刻停下", "本轮到期停在实际截止时刻"],
)
def test_ride_deadline_does_not_create_backdated_motion_or_events(
    ride_world, monkeypatch, previous_minutes, current_minutes, expected_minutes, expect_progress
):
    e = ride_world
    with e.db.write() as c:
        c.execute(
            "UPDATE locations SET longitude=?,latitude=? WHERE id=?",
            (e.loc["longitude"] + 0.003, e.loc["latitude"], e.destination),
        )
    ride_id = boarded(
        e, member_ids=[e.player, e.npc, e.other],
        deadline_world_time=e.at + timedelta(minutes=5),
    )
    install_known_route(e, monkeypatch)
    choose(e, e.player, "depart", ride_id)
    previous = e.at + timedelta(minutes=previous_minutes)
    current = e.at + timedelta(minutes=current_minutes)
    expected_stop = e.at + timedelta(minutes=expected_minutes)

    with e.db.write() as c:
        # 哥哥，旧存档可能已越过截止时间，但仍保留旧版的 moving 状态。
        slow_real_routes(c, e.wid)
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(previous), e.wid))
        before = tuple(c.execute(
            "SELECT longitude,latitude,distance_travelled_km FROM vehicle_states WHERE vehicle_id=?",
            (e.vehicle,),
        ).fetchone())
        MovementService().advance(
            c, world_id=e.wid, previous_time=previous, current_time=current, created_at=utc_now(),
        )
        ride = c.execute("SELECT * FROM vehicle_rides WHERE id=?", (ride_id,)).fetchone()
        movement = c.execute("SELECT * FROM character_movements WHERE id=?", (ride["movement_id"],)).fetchone()
        event = c.execute("SELECT event_type,occurred_at FROM world_events WHERE id=?", (ride["source_event_id"],)).fetchone()
        vehicle = c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()
        assert ride["status"] == "blocked" and movement["status"] == "suspended"
        assert event["event_type"] == "social.ride_blocked"
        assert from_iso(event["occurred_at"]) == expected_stop
        assert from_iso(vehicle["updated_at"]) == expected_stop
        assert from_iso(movement["updated_at_world"]) == expected_stop
        assert bool(vehicle["distance_travelled_km"] > before[2]) is expect_progress
        if not expect_progress:
            assert (vehicle["longitude"], vehicle["latitude"]) == before[:2]
        for person_id in (e.player, e.npc, e.other):
            person = c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (person_id,)).fetchone()
            assert (person["longitude"], person["latitude"]) == (vehicle["longitude"], vehicle["latitude"])
        assert c.execute(
            "SELECT COUNT(*) FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL",
            (ride_id,),
        ).fetchone()[0] == 3
        assert not c.execute(
            "SELECT 1 FROM world_events WHERE event_type='action.movement_arrived' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement["id"],),
        ).fetchone()
