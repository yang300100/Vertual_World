"""哥哥，结束原旅行后，独立住宿仍约束下一趟真实路线及赶回房间的时间。"""

# ruff: noqa: F811
from datetime import timedelta

import pytest
from test_appointments import people
from test_journeys import advance_to_arrival
from test_journeys import choose as journey_choice
from test_journeys import get as journey
from test_lived_world import definition, town  # noqa: F401
from test_overnight_stays import (
    accepted_stay,
    current_time,
    get,
    room_at_stop,
    stopped_long_journey,
)
from test_routines import register

from world_engine.journeys import JourneyIntent, JourneyService
from world_engine.repository import to_iso


def replacement_trip(e, room_id, *, minutes=120, return_to_origin=False):
    at = current_time(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        origin = c.execute("SELECT location_id FROM life_rooms WHERE id=?", (room_id,)).fetchone()[
            0
        ]
        return JourneyService.apply(
            c,
            npc,
            player,
            at,
            JourneyIntent(
                operation="propose",
                meeting_location_id=origin,
                deadline_world_time=to_iso(at + timedelta(hours=4)),
                stops=[{"location_id": e.loc["id"], "minutes": minutes}],
                return_to_origin=return_to_origin,
                statement="结束上一趟之后，我独立提出这份有限路线，既有住宿仍需守约。",
            ),
        )["journey_id"]


def test_independent_accepted_overnight_rejects_conflicting_replacement_trip(town):
    e = town
    original = stopped_long_journey(e)
    room_id = room_at_stop(e, original)
    stay_id = accepted_stay(e, original, room_id)
    assert journey_choice(e, "end", original).status_code == 200
    assert get(e, stay_id)["status"] == "accepted"

    replacement = replacement_trip(e, room_id)
    assert journey_choice(e, "accept", replacement).status_code == 200
    result = journey_choice(e, "depart", replacement)
    assert result.status_code == 409, result.text
    assert "住宿" in result.json()["detail"]
    assert journey(e, replacement)["status"] == "accepted"
    assert get(e, stay_id)["status"] == "accepted"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()


def test_feasible_replacement_trip_really_completes_before_independent_overnight(town):
    e = town
    original = stopped_long_journey(e)
    room_id = room_at_stop(e, original)
    stay_id = accepted_stay(
        e,
        original,
        room_id,
        arrival_world_time=to_iso(current_time(e) + timedelta(hours=2)),
    )
    assert journey_choice(e, "end", original).status_code == 200

    replacement = replacement_trip(e, room_id, minutes=2, return_to_origin=True)
    assert journey_choice(e, "accept", replacement).status_code == 200
    assert journey_choice(e, "depart", replacement).status_code == 200
    assert advance_to_arrival(e, replacement)["phase"] == "stopped"
    e.clock.heartbeat(e.wid, elapsed_seconds=120)
    assert journey_choice(e, "depart", replacement).status_code == 200
    assert advance_to_arrival(e, replacement)["status"] == "completed"
    assert get(e, stay_id)["status"] == "accepted"
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM journey_legs WHERE journey_id=? AND status='arrived'",
                (replacement,),
            ).fetchone()[0]
            == 2
        )


def test_finishing_trip_before_arrival_still_requires_onward_road_and_door_time(town):
    e = town
    original = stopped_long_journey(e)
    room_id = room_at_stop(e, original)
    at = current_time(e)
    with e.db.read() as c:
        participants = people(e, c)
        outbound = JourneyService._estimate(
            c,
            participants,
            [{"location_id": e.loc["id"], "minutes": 0}],
            [(p["longitude"], p["latitude"]) for p in participants],
        )
    # 哥哥，表面上路线已能先结束，但只留下五分钟，仍来不及赶回并逐门入屋。
    arrival = at + timedelta(seconds=outbound, minutes=5)
    stay_id = accepted_stay(e, original, room_id, arrival_world_time=to_iso(arrival))
    assert journey_choice(e, "end", original).status_code == 200
    replacement = replacement_trip(e, room_id, minutes=0)
    assert journey_choice(e, "accept", replacement).status_code == 200
    assert at + timedelta(seconds=outbound) < arrival
    result = journey_choice(e, "depart", replacement)
    assert result.status_code == 409, result.text
    assert "住宿" in result.json()["detail"]
    assert get(e, stay_id)["status"] == "accepted"


@pytest.mark.parametrize("source", ["ordinary_work", "routine", "goal", "direct_task"])
@pytest.mark.parametrize("arrival_minutes", [30, 120])
def test_npc_work_preserves_overnight_departure_time(town, source, arrival_minutes):
    from world_engine.activity_tasks import TaskService
    from world_engine.daily_life import DailyLifeService
    from world_engine.interiors import InteriorService
    from world_engine.life import LifeActivityError, LifeActivityService
    from world_engine.npc_goals import NpcGoalService
    from world_engine.routines import RoutineService

    e = town
    original = stopped_long_journey(e)
    room_id = room_at_stop(e, original)
    stay_id = accepted_stay(
        e,
        original,
        room_id,
        window_minutes=15,
        arrival_world_time=to_iso(current_time(e) + timedelta(minutes=arrival_minutes)),
    )
    at = current_time(e)
    with e.db.write() as c:
        _, npc = people(e, c)
        location_id = c.execute(
            "SELECT location_id FROM life_rooms WHERE id=?", (room_id,)
        ).fetchone()[0]
        # 哥哥，只沿本人已有许可的真实门进出，核实当前地点，不直接重写坐标或同意。
        for operation in ("open", "enter", "exit"):
            npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            InteriorService.door(c, npc, room_id, operation, at)
    definition(
        e,
        {
            "element_type": "workplace_budget",
            "location_id": location_id,
            "initial_funds": 5000,
            "wage": 12,
        },
    )
    if source in {"routine", "goal"}:
        register(
            e,
            {
                "name": "真实住宿前的工作边界",
                "character_id": e.npc,
                "slots": [
                    {
                        "key": "work",
                        "name": "日常工作",
                        "kind": "work",
                        "starts_at": (at - timedelta(minutes=1)).strftime("%H:%M")
                        if source == "routine"
                        else "13:00",
                        "duration_minutes": 180,
                        "location_id": location_id,
                    }
                ],
                "ambitions": [
                    {
                        "key": "reserve",
                        "title": "挣取真实生活储备",
                        "kind": "reserve_money",
                        "target": 1000,
                        "work_location_ids": [location_id],
                    }
                ]
                if source == "goal"
                else [],
            },
        )
    with e.db.write() as c:
        _, npc = people(e, c)
        if source == "ordinary_work":
            location = c.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
            DailyLifeService._find_work(c, npc, at, [location])
        elif source == "routine":
            RoutineService.perform(c, npc, at, RoutineService.context(c, npc, at), None)
        elif source == "goal":
            NpcGoalService.perform(c, npc, at, RoutineService.context(c, npc, at), None)
        else:
            if arrival_minutes == 30:
                with pytest.raises(LifeActivityError, match="住宿|赴约"):
                    TaskService.start(c, npc, at)
            else:
                TaskService.start(c, npc, at)
        running = LifeActivityService.running(c, e.npc)
        if arrival_minutes == 30:
            assert running is None
        else:
            assert running is not None and running["kind"] == "work"
    assert get(e, stay_id)["status"] == "accepted"
