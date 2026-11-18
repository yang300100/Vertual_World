"""哥哥，三人真实同行中的个人退出、继续前进和移动归属边界。"""

# ruff: noqa: F811

from datetime import timedelta

import pytest
from test_companion_consent import accepted, choose
from test_lived_world import town  # noqa: F401
from test_overnight_options import third_person

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.companion_proposals import CompanionIntent, CompanionProposalService
from world_engine.companions import CompanionService
from world_engine.movement import MovementService
from world_engine.player_steering import PlayerSteeringService
from world_engine.repository import to_iso, utc_now
from world_engine.schedules import ScheduleService


def group(e):
    third = third_person(e)
    with e.db.write() as c:
        for cid, offset in ((e.npc, 0.00003), (third, -0.00003)):
            c.execute(
                "UPDATE characters SET longitude=?,latitude=?,location_id=?,current_location_id=?,"
                "movement_type='land',movement_speed_kmh=3,active_vehicle_id=NULL,"
                "current_room_id=NULL,current_fixture_id=NULL,traits_json='[]' WHERE id=?",
                (e.loc["longitude"] + offset, e.loc["latitude"], e.loc["id"], e.loc["id"], cid),
            )
    _, first = accepted(e, 30)
    pid = choose(e, minutes=30, other_npc=third)["proposal_id"]
    second = choose(e, "accept", pid, npc=True, other_npc=third)
    return third, first["session_id"], second["session_id"]


def start(e, c, mover=None):
    return (mover or MovementService()).start(
        c,
        world_id=e.wid,
        character_id=e.player,
        world_time=e.at,
        destination_longitude=e.loc["longitude"] + 0.003,
        destination_latitude=e.loc["latitude"],
    )


def positions(c, ids):
    return {
        cid: tuple(
            c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (cid,)).fetchone()
        )
        for cid in ids
    }


def advance(e, c, previous, current):
    MovementService().advance(
        c,
        world_id=e.wid,
        previous_time=previous,
        current_time=current,
        created_at=utc_now(),
    )


@pytest.mark.parametrize(
    "exit_kind", ["npc_end", "player_end", "energy", "satiety", "health", "schedule", "independent"]
)
def test_one_person_exits_and_other_two_really_arrive(town, exit_kind):
    e = town
    third, first_sid, second_sid = group(e)
    with e.db.write() as c:
        movement = start(e, c)
        initial = {
            row["character_id"]: dict(row)
            for row in c.execute("SELECT * FROM character_movements WHERE status='moving'")
        }
        assert set(initial) == {e.player, e.npc, third}
        assert len({row["origin_longitude"] for row in initial.values()}) == 3
        assert c.execute("SELECT COUNT(*) FROM companion_movement_links").fetchone()[0] == 2
        midway = e.at + timedelta(minutes=1)
        advance(e, c, e.at, midway)
        before = positions(c, initial)
        assert all(before[cid][0] > row["origin_longitude"] for cid, row in initial.items())
        before_movements = {
            cid: dict(
                c.execute("SELECT * FROM character_movements WHERE id=?", (row["id"],)).fetchone()
            )
            for cid, row in initial.items()
        }
        remaining_agreement = tuple(
            c.execute("SELECT * FROM companion_sessions WHERE id=?", (second_sid,)).fetchone()
        )
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        if exit_kind in {"npc_end", "player_end"}:
            CompanionProposalService.apply(
                c,
                npc if exit_kind == "npc_end" else player,
                player if exit_kind == "npc_end" else npc,
                midway,
                CompanionIntent(
                    operation="end", session_id=first_sid, statement="我明确结束这份同行。"
                ),
            )
        elif exit_kind == "independent":
            independent = MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.npc,
                world_time=midway,
                destination_longitude=e.loc["longitude"] - 0.002,
                destination_latitude=e.loc["latitude"],
            )
            assert independent.origin_longitude == before[e.npc][0]
            assert independent.origin_latitude == before[e.npc][1]
        else:
            if exit_kind == "schedule":
                # 哥哥，途中新增的安排仍经两人明确确认，再让真实日程触发个人退出。
                offered = AppointmentService.apply(
                    c,
                    npc,
                    player,
                    midway,
                    AppointmentIntent(
                        operation="propose",
                        kind="talk",
                        location_id=e.loc["id"],
                        meeting_world_time=to_iso(e.at + timedelta(minutes=15)),
                        statement="十五分钟时回来见面，我们留出准备和赶路的时间。",
                    ),
                )
                AppointmentService.apply(
                    c,
                    player,
                    npc,
                    midway,
                    AppointmentIntent(
                        operation="accept",
                        appointment_id=offered["appointment_id"],
                        statement="我明确接受这次另行安排的见面。",
                    ),
                )
                assert ScheduleService.assess(c, npc, midway)["due"]
            else:
                field, value = ("health", 0) if exit_kind == "health" else (exit_kind, 10)
                c.execute(f"UPDATE characters SET {field}=? WHERE id=?", (value, e.npc))
            CompanionService.maintain(c, e.wid, midway)
        assert positions(c, initial) == before
        assert (
            c.execute("SELECT status FROM companion_sessions WHERE id=?", (first_sid,)).fetchone()[
                0
            ]
            == "ended"
        )
        assert (
            tuple(
                c.execute("SELECT * FROM companion_sessions WHERE id=?", (second_sid,)).fetchone()
            )
            == remaining_agreement
        )
        for cid in (e.player, third):
            assert (
                dict(
                    c.execute(
                        "SELECT * FROM character_movements WHERE id=?", (initial[cid]["id"],)
                    ).fetchone()
                )
                == before_movements[cid]
            )
            assert (
                c.execute(
                    "SELECT status FROM character_movements WHERE id=?", (initial[cid]["id"],)
                ).fetchone()[0]
                == "moving"
            )
        assert (
            c.execute(
                "SELECT status FROM character_movements WHERE id=?", (initial[e.npc]["id"],)
            ).fetchone()[0]
            == "cancelled"
        )
        arrived = movement.estimated_arrival_world
        advance(e, c, midway, arrived)
        for cid in (e.player, third):
            row = c.execute(
                "SELECT * FROM character_movements WHERE id=?", (initial[cid]["id"],)
            ).fetchone()
            assert row["status"] == "arrived"
            assert positions(c, [cid])[cid] == (
                row["destination_longitude"],
                row["destination_latitude"],
            )
        if exit_kind != "independent":
            assert positions(c, [e.npc])[e.npc] == before[e.npc]
        assert {s["companion_id"] for s in CompanionService.active(c, e.player)} == {third}


@pytest.mark.parametrize("independent_exit", [False, True])
def test_leader_cancel_stops_bound_members_and_preserves_departed_person_new_route(
    town, independent_exit
):
    e = town
    third, _, _ = group(e)
    with e.db.write() as c:
        start(e, c)
        midway = e.at + timedelta(minutes=1)
        advance(e, c, e.at, midway)
        independent = None
        if independent_exit:
            independent = MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.npc,
                world_time=midway,
                destination_longitude=e.loc["longitude"] - 0.002,
                destination_latitude=e.loc["latitude"],
            )
        before = positions(c, [e.player, e.npc, third])
        MovementService().cancel(c, world_id=e.wid, character_id=e.player, world_time=midway)
        assert positions(c, before) == before
        still_moving = c.execute(
            "SELECT id,character_id FROM character_movements WHERE status='moving'"
        ).fetchall()
        assert [(r["id"], r["character_id"]) for r in still_moving] == (
            [(independent.id, e.npc)] if independent else []
        )


def test_ended_session_replay_does_not_cancel_new_independent_movement_or_duplicate_history(town):
    e = town
    group(e)
    with e.db.write() as c:
        start(e, c)
        old = c.execute(
            "SELECT * FROM companion_sessions WHERE companion_id=? AND status='active'", (e.npc,)
        ).fetchone()
        independent = MovementService().start(
            c,
            world_id=e.wid,
            character_id=e.npc,
            world_time=e.at,
            destination_longitude=e.loc["longitude"] - 0.002,
            destination_latitude=e.loc["latitude"],
        )
        before = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        CompanionService.end(c, old, e.at, "重放旧会话的退出请求。")
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == before
        assert (
            c.execute(
                "SELECT status FROM character_movements WHERE id=?", (independent.id,)
            ).fetchone()[0]
            == "moving"
        )


@pytest.mark.parametrize("new_movement_kind", ["planned", "steering"])
def test_old_session_replay_preserves_leader_new_independent_route_or_input(
    town, new_movement_kind
):
    e = town
    group(e)
    with e.db.write() as c:
        start(e, c)
        old_sessions = list(CompanionService.active(c, e.player))
        MovementService().cancel(c, world_id=e.wid, character_id=e.player, world_time=e.at)
        for session in old_sessions:
            CompanionService.end(c, session, e.at, "本人先结束旧同行，再独立出发。")
        if new_movement_kind == "planned":
            independent = MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.player,
                world_time=e.at,
                destination_longitude=e.loc["longitude"] - 0.002,
                destination_latitude=e.loc["latitude"],
            )
            before = tuple(
                c.execute(
                    "SELECT * FROM character_movements WHERE id=?", (independent.id,)
                ).fetchone()
            )
        else:
            player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
            PlayerSteeringService.set_direction(c, player, (-1, 0), 101, e.at, utc_now())
            before = tuple(PlayerSteeringService.active(c, e.player))
        event_count = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        for session in old_sessions:
            CompanionService.end(c, session, e.at, "重放已经结束的旧同行退出。")
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == event_count
        if new_movement_kind == "planned":
            assert (
                tuple(
                    c.execute(
                        "SELECT * FROM character_movements WHERE id=?", (independent.id,)
                    ).fetchone()
                )
                == before
            )
        else:
            assert tuple(PlayerSteeringService.active(c, e.player)) == before


def test_migration_never_guesses_old_movement_bindings(town):
    e = town
    _, sid, _ = group(e)
    with e.db.write() as c:
        start(e, c)
        c.execute("DROP TABLE companion_movement_links")
    e.db.initialize()
    e.db.initialize()
    with e.db.write() as c:
        assert not c.execute("SELECT 1 FROM companion_movement_links").fetchone()
        session = c.execute("SELECT * FROM companion_sessions WHERE id=?", (sid,)).fetchone()
        before = positions(c, [e.player, e.npc])
        CompanionService.end(c, session, e.at, "历史未绑定会话结束，不猜测已有移动的归属。")
        assert positions(c, before) == before
        assert (
            c.execute("SELECT COUNT(*) FROM character_movements WHERE status='moving'").fetchone()[
                0
            ]
            == 3
        )


@pytest.mark.parametrize("failure_point", ["second_route", "after_binding"])
def test_group_departure_rolls_back_all_movement_links_and_history(
    town, monkeypatch, failure_point
):
    e = town
    group(e)
    mover = MovementService()
    if failure_point == "second_route":
        original = mover._start_solo

        def fail_second(c, **kwargs):
            if kwargs["character_id"] == e.npc:
                raise ValueError("隔离测试：第二人的真实路线不可达")
            return original(c, **kwargs)

        monkeypatch.setattr(mover, "_start_solo", fail_second)
    else:
        original_binding = CompanionService.bind_movements

        def fail_binding(cls, c, leader, movements):
            original_binding(c, leader, movements)
            raise ValueError("隔离测试：绑定写入后失败")

        monkeypatch.setattr(CompanionService, "bind_movements", classmethod(fail_binding))
    with e.db.write() as c:
        tables = (
            "character_movements",
            "companion_movement_links",
            "world_events",
            "character_memories",
        )
        before = {
            table: [tuple(r) for r in c.execute(f"SELECT * FROM {table}")] for table in tables
        }
        with pytest.raises(ValueError, match="隔离测试"):
            start(e, c, mover)
        assert {
            table: [tuple(r) for r in c.execute(f"SELECT * FROM {table}")] for table in tables
        } == before


def test_personal_exit_keeps_other_two_steering_from_their_real_positions(town):
    e = town
    third, sid, _ = group(e)
    now = utc_now()
    with e.db.write() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        PlayerSteeringService.set_direction(c, player, (1, 0), 100, e.at, now)
        PlayerSteeringService.advance(c, e.wid, e.at + timedelta(seconds=1), now)
        before = positions(c, [e.player, e.npc, third])
        session = c.execute("SELECT * FROM companion_sessions WHERE id=?", (sid,)).fetchone()
        CompanionService.end(c, session, e.at + timedelta(seconds=1), "本人选择退出方向同行。")
        assert PlayerSteeringService.active(c, e.player) is not None
        PlayerSteeringService.advance(c, e.wid, e.at + timedelta(seconds=2), now)
        after = positions(c, before)
        assert after[e.npc] == before[e.npc]
        assert all(after[cid][0] > before[cid][0] for cid in (e.player, third))


@pytest.mark.parametrize("movement_kind", ["planned", "steering"])
def test_unavailable_leader_stops_own_bound_group_instead_of_continuing_after_death(
    town, movement_kind
):
    e = town
    third, _, _ = group(e)
    with e.db.write() as c:
        if movement_kind == "planned":
            start(e, c)
        else:
            player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
            PlayerSteeringService.set_direction(c, player, (1, 0), 100, e.at, utc_now())
        before = positions(c, [e.player, e.npc, third])
        c.execute("UPDATE characters SET health=0 WHERE id=?", (e.player,))
        CompanionService.maintain(c, e.wid, e.at)
        assert positions(c, before) == before
        assert not c.execute("SELECT 1 FROM character_movements WHERE status='moving'").fetchone()
        assert PlayerSteeringService.active(c, e.player) is None
        assert not CompanionService.active(c, e.player)
