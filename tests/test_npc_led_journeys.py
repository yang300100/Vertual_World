"""哥哥，NPC 带路必须有本人有限授权，真实路线与随时退出都要保留。"""

# ruff: noqa: F811
import json
import sqlite3
from datetime import timedelta

import pytest
from test_appointments import people
from test_journeys import advance_to_arrival, choose, get, npc_choice, offer, sites
from test_lived_world import town  # noqa: F401

from world_engine.intimacy import IntimacyService, RelationshipIntent
from world_engine.journey_follow import JourneyFollowService
from world_engine.journeys import JourneyIntent, JourneyService
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso


def now(e, c):
    return from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
    )


def player_choice(e, jid, operation):
    with e.db.write() as c:
        player, npc = people(e, c)
        return JourneyService.apply(
            c,
            player,
            npc,
            now(e, c),
            JourneyIntent(
                operation=operation,
                journey_id=jid,
                statement="这是我本人对这份有限行程的明确选择。",
            ),
        )


def led_offer(e, **kwargs):
    jid = offer(e, leader_id=e.npc, **kwargs)
    assert choose(e, "accept", jid).status_code == 200
    return jid


def follow(e, jid):
    return player_choice(e, jid, "follow")


def start(e, jid):
    follow(e, jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, jid)["phase"] == "moving"


def no_player_move(e, c):
    return (
        c.execute("SELECT 1 FROM character_movements WHERE character_id=?", (e.player,)).fetchone()
        is None
    )


def test_accept_and_daily_life_never_authorize_player_movement(town):
    e = town
    jid = led_offer(e)
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    row = get(e, jid)
    assert row["status"] == "accepted" and row["follow_authorized_by"] is None
    with e.db.read() as c:
        assert no_player_move(e, c)
        assert not JourneyFollowService.valid(c, row, now(e, c))
    with pytest.raises(ValueError, match="有效跟随"):
        npc_choice(e, jid, "depart")


def test_follow_cannot_replace_the_other_persons_acceptance(town):
    e = town
    jid = offer(e, leader_id=e.npc)
    with pytest.raises(ValueError, match="共同确认"):
        follow(e, jid)
    assert get(e, jid)["status"] == "pending"
    assert choose(e, "decline", jid).status_code == 200
    with pytest.raises(ValueError, match="共同确认"):
        follow(e, jid)
    with e.db.read() as c:
        assert no_player_move(e, c)
        assert not c.execute(
            "SELECT 1 FROM world_events WHERE event_type='social.appointment_journey_follow'"
        ).fetchone()


def test_follow_records_player_consent_without_moving_or_spending_and_retries_reuse(town):
    e = town
    jid = led_offer(e)
    with e.db.read() as c:
        before = [
            tuple(p)
            for p in c.execute("SELECT id,longitude,latitude,money FROM characters ORDER BY id")
        ]
    result = follow(e, jid)
    assert follow(e, jid)["event_id"] == result["event_id"]
    row = get(e, jid)
    with e.db.read() as c:
        event = c.execute("SELECT * FROM world_events WHERE id=?", (result["event_id"],)).fetchone()
        assert event["actor_id"] == e.player and event["target_id"] == e.npc
        assert event["event_type"] == "social.appointment_journey_follow"
        assert json.loads(event["payload_json"])["follow_terms"] == JourneyFollowService.terms(
            c, row
        )
        assert [
            tuple(p)
            for p in c.execute("SELECT id,longitude,latitude,money FROM characters ORDER BY id")
        ] == before
        assert no_player_move(e, c)
        assert JourneyFollowService.valid(c, row, now(e, c))
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_events "
                "WHERE event_type='social.appointment_journey_follow'"
            ).fetchone()[0]
            == 1
        )


def test_only_player_can_follow_and_only_agreed_npc_leader_can_depart(town):
    e = town
    jid = led_offer(e)
    with pytest.raises(ValueError, match="只有玩家本人"):
        npc_choice(e, jid, "follow")
    follow(e, jid)
    with pytest.raises(ValueError, match="领队本人"):
        player_choice(e, jid, "depart")
    assert npc_choice(e, jid, "depart")["status"] == "active"


def test_player_led_trip_rejects_follow_and_keeps_existing_manual_departure(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    with pytest.raises(ValueError, match="NPC 领队"):
        follow(e, jid)
    assert choose(e, "depart", jid).status_code == 200


def test_clock_drives_distinct_real_routes_dwell_next_leg_and_return(town):
    e = town
    jid = led_offer(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.0001 WHERE id=?", (e.npc,))
    start(e, jid)
    with e.db.read() as c:
        moves = c.execute(
            "SELECT * FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchall()
        assert len(moves) == 2 and len({m["origin_longitude"] for m in moves}) == 2
        assert len({m["estimated_arrival_world"] for m in moves}) == 1
        assert all(m["speed_kmh"] <= 3 for m in moves)
    first = advance_to_arrival(e, jid)
    assert first["phase"] == "stopped" and first["next_index"] == 1
    e.clock.heartbeat(e.wid, elapsed_seconds=119)
    assert get(e, jid)["phase"] == "stopped"
    e.clock.heartbeat(e.wid, elapsed_seconds=2)
    assert get(e, jid)["phase"] == "moving"
    final = advance_to_arrival(e, jid)
    assert final["status"] == "completed" and final["follow_authorized_by"] is None
    with e.db.read() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM journey_legs WHERE status='arrived'").fetchone()[0] == 2
        )
        assert (
            c.execute("SELECT COUNT(*) FROM journey_stops WHERE arrived_at IS NOT NULL").fetchone()[
                0
            ]
            == 2
        )
        for cid in (e.player, e.npc):
            assert c.execute("SELECT longitude FROM characters WHERE id=?", (cid,)).fetchone()[
                0
            ] == pytest.approx(e.loc["longitude"])


@pytest.mark.parametrize("operation", ["withdraw_follow", "pause", "end"])
def test_stop_keeps_real_midway_positions_and_clears_future_authority(town, operation):
    e = town
    jid = led_offer(e)
    start(e, jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    with e.db.read() as c:
        before = [
            tuple(p) for p in c.execute("SELECT id,longitude,latitude FROM characters ORDER BY id")
        ]
    result = player_choice(e, jid, operation)
    assert result["status"] == ("ended" if operation == "end" else "paused")
    assert get(e, jid)["follow_authorized_by"] is None
    with e.db.read() as c:
        assert [
            tuple(p) for p in c.execute("SELECT id,longitude,latitude FROM characters ORDER BY id")
        ] == before
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()


def test_bilateral_resume_does_not_restore_follow_authority(town):
    e = town
    jid = led_offer(e)
    start(e, jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=30)
    player_choice(e, jid, "withdraw_follow")
    assert player_choice(e, jid, "resume")["status"] == "resume_pending"
    assert npc_choice(e, jid, "resume")["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=30)
    assert get(e, jid)["phase"] == "ready" and get(e, jid)["follow_authorized_by"] is None
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()
    follow(e, jid)
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, jid)["phase"] == "moving"


@pytest.mark.parametrize(
    "changed", ["route", "deadline", "leader", "event_actor", "event_payload", "missing_event"]
)
def test_changed_terms_or_forged_authorization_prevent_further_departure(town, changed):
    e = town
    jid = led_offer(e)
    follow(e, jid)
    with e.db.write() as c:
        row = c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone()
        if changed == "route":
            c.execute(
                "UPDATE journey_stops SET minutes=minutes+1 WHERE journey_id=? AND stop_index=0",
                (jid,),
            )
        elif changed == "deadline":
            c.execute(
                "UPDATE joint_journeys SET deadline_at=? WHERE id=?",
                (to_iso(e.at + timedelta(hours=5)), jid),
            )
        elif changed == "leader":
            c.execute("UPDATE joint_journeys SET leader_id=? WHERE id=?", (e.player, jid))
        elif changed == "event_actor":
            c.execute(
                "UPDATE world_events SET actor_id=? WHERE id=?",
                (e.npc, row["follow_source_event_id"]),
            )
        elif changed == "event_payload":
            c.execute(
                "UPDATE world_events SET payload_json='{}' WHERE id=?",
                (row["follow_source_event_id"],),
            )
        else:
            c.execute("UPDATE joint_journeys SET follow_source_event_id=NULL WHERE id=?", (jid,))
        JourneyService.maintain(c, e.wid, now(e, c))
        assert no_player_move(e, c)
    assert get(e, jid)["status"] == "paused" and get(e, jid)["follow_authorized_by"] is None


@pytest.mark.parametrize("problem", ["hungry", "tired", "hurt", "contact", "deadline"])
def test_predeparture_changes_clear_follow_and_preserve_player_money(town, problem):
    e = town
    jid = led_offer(e)
    follow(e, jid)
    with e.db.write() as c:
        at = now(e, c)
        money = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
        if problem == "hungry":
            c.execute("UPDATE characters SET satiety=30 WHERE id=?", (e.npc,))
        elif problem == "tired":
            c.execute("UPDATE characters SET energy=20 WHERE id=?", (e.player,))
        elif problem == "hurt":
            c.execute("UPDATE characters SET health=39 WHERE id=?", (e.npc,))
        elif problem == "contact":
            player, npc = people(e, c)
            IntimacyService.apply(
                c,
                player,
                npc,
                at,
                RelationshipIntent(
                    operation="boundary", pause_contact=True, statement="我想暂停联系。"
                ),
            )
        else:
            at = from_iso(get(e, jid)["deadline_at"])
        JourneyService.maintain(c, e.wid, at)
        assert no_player_move(e, c)
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == money
        )
    assert get(e, jid)["status"] == ("expired" if problem == "deadline" else "paused")
    assert get(e, jid)["follow_authorized_by"] is None


def test_newly_confirmed_appointment_releases_predeparture_follow_at_due_boundary(town):
    from test_appointments import choose as appointment_choose
    from test_appointments import offer as appointment_offer

    from world_engine.schedules import ScheduleService

    e = town
    jid = led_offer(e)
    follow(e, jid)
    aid = appointment_offer(e)
    assert appointment_choose(e, "accept", aid).status_code == 200
    with e.db.write() as c:
        plans = [ScheduleService.assess(c, person, e.at) for person in people(e, c)]
        due = min(plan["depart_at"] for plan in plans if plan)
        JourneyService.maintain(c, e.wid, due)
        assert no_player_move(e, c)
    row = get(e, jid)
    assert row["status"] == "paused" and row["follow_authorized_by"] is None


def test_failed_second_route_rolls_back_then_automatic_departure_pauses(town, monkeypatch):
    e = town
    jid = led_offer(e)
    follow(e, jid)
    original = MovementService._start_solo

    def blocked(self, c, **kwargs):
        if kwargs["character_id"] == e.npc:
            raise ValueError("第二人的真实路线临时不可通行")
        return original(self, c, **kwargs)

    monkeypatch.setattr(MovementService, "_start_solo", blocked)
    with pytest.raises(ValueError, match="临时不可通行"):
        npc_choice(e, jid, "depart")
    assert get(e, jid)["status"] == "accepted"
    with e.db.write() as c:
        assert not c.execute("SELECT 1 FROM journey_legs").fetchone()
        assert not c.execute("SELECT 1 FROM character_movements").fetchone()
        assert JourneyService.perform(c, people(e, c)[1], now(e, c))
        assert not c.execute("SELECT 1 FROM journey_legs").fetchone()
        assert not c.execute("SELECT 1 FROM character_movements").fetchone()
    assert get(e, jid)["status"] == "paused" and get(e, jid)["follow_authorized_by"] is None


def test_player_request_does_not_probe_npc_private_visits_but_npc_accept_checks_self(town):
    e = town
    place = sites(e)[0]
    with e.db.write() as c:
        c.execute(
            "DELETE FROM event_observers WHERE character_id=? "
            "AND event_id IN (SELECT id FROM world_events WHERE location_id=?)",
            (e.npc, place),
        )
        player, npc = people(e, c)
        result = JourneyService.apply(
            c,
            player,
            npc,
            e.at,
            JourneyIntent(
                operation="propose",
                leader_id=e.npc,
                meeting_location_id=e.loc["id"],
                deadline_world_time=to_iso(e.at + timedelta(hours=4)),
                stops=[{"location_id": place, "minutes": 2}],
                statement="请你带我去我曾经到过的地方，好吗？",
            ),
        )
        assert result["status"] == "pending"
    with pytest.raises(ValueError, match="领队本人尚不能确认"):
        npc_choice(e, result["journey_id"], "accept")
    assert get(e, result["journey_id"])["status"] == "pending"
    assert npc_choice(e, result["journey_id"], "decline")["status"] == "declined"


def test_npc_proposal_still_requires_own_real_visits_and_third_party_cannot_lead(town):
    e = town
    place = sites(e)[0]
    with e.db.write() as c:
        stranger = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    with pytest.raises(ValueError, match="本次领队必须"):
        offer(e, stops=[{"location_id": place, "minutes": 2}], leader_id=stranger)
    with e.db.write() as c:
        c.execute(
            "DELETE FROM event_observers WHERE character_id=? "
            "AND event_id IN (SELECT id FROM world_events WHERE location_id=?)",
            (e.npc, place),
        )
    with pytest.raises(ValueError, match="到访"):
        offer(e, stops=[{"location_id": place, "minutes": 2}], leader_id=e.npc)


def test_shared_rows_mask_npc_name_and_do_not_publish_authorization_terms(town):
    e = town
    jid = led_offer(e)
    with e.db.read() as c:
        npc_name = people(e, c)[1]["name"]
        row = next(r for r in JourneyService.rows(c, e.wid, e.player, now(e, c)) if r["id"] == jid)
        assert row["leader_name"] != npc_name
        assert row["can_follow"] and not row["following"]
        assert "follow_terms_json" not in row
    follow(e, jid)
    with e.db.write() as c:
        c.execute("UPDATE characters SET health=39 WHERE id=?", (e.npc,))
        row = next(r for r in JourneyService.rows(c, e.wid, e.player, now(e, c)) if r["id"] == jid)
        assert row["following"] and not row["can_follow"]


def test_old_null_leader_migration_is_idempotent_without_follow_backfill(town):
    e = town
    jid = offer(e)
    choose(e, "accept", jid)
    # 哥哥，只在临时数据库移除新列，还原本阶段之前的真实旧表结构。
    with sqlite3.connect(e.settings.database_path) as c:
        c.execute("DROP INDEX idx_journey_follow_due")
        for name in (
            "follow_authorized_by",
            "follow_authorized_at",
            "follow_source_event_id",
            "follow_terms_json",
            "leader_id",
        ):
            c.execute(f"ALTER TABLE joint_journeys DROP COLUMN {name}")
    e.db.initialize()
    e.db.initialize()
    row = get(e, jid)
    assert row["leader_id"] is None and row["follow_authorized_by"] is None
    with e.db.read() as c:
        assert JourneyFollowService.leader_id(c, row) == e.player
        assert not JourneyFollowService.valid(c, row, now(e, c))
    assert choose(e, "depart", jid).status_code == 200


@pytest.mark.parametrize("who", ["player", "npc"])
def test_independent_predeparture_move_revokes_follow_and_old_trip_does_not_cancel_it(town, who):
    e = town
    jid = led_offer(e)
    follow(e, jid)
    with e.db.write() as c:
        move = MovementService().start(
            c,
            world_id=e.wid,
            character_id=getattr(e, who),
            world_time=now(e, c),
            destination_longitude=e.loc["longitude"] - 0.003,
            destination_latitude=e.loc["latitude"],
        )
        row = c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "paused" and row["follow_authorized_by"] is None
    player_choice(e, jid, "end")
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM character_movements WHERE id=?", (move.id,)).fetchone()[0]
            == "moving"
        )


def test_invalid_independent_move_does_not_revoke_predeparture_follow(town):
    e = town
    jid = led_offer(e)
    result = follow(e, jid)
    with e.db.write() as c:
        with pytest.raises(ValueError):
            MovementService().start(
                c,
                world_id=e.wid,
                character_id=e.npc,
                world_time=now(e, c),
                destination_longitude=999,
                destination_latitude=e.loc["latitude"],
            )
        row = c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "accepted" and row["follow_source_event_id"] == result["event_id"]
        assert JourneyFollowService.valid(c, row, now(e, c))


@pytest.mark.parametrize("source", ["refresh", "worker"])
def test_large_heartbeat_runs_first_departure_dwell_and_later_legs_without_secondly_loop(
    town, source
):
    e = town
    destinations = sites(e)
    jid = led_offer(e, stops=[{"location_id": lid, "minutes": 2} for lid in destinations])
    follow(e, jid)
    worker = None
    try:
        if source == "refresh":
            e.clock.heartbeat(e.wid, elapsed_seconds=7200)
        else:
            from world_engine.worker import WorldWorker

            worker = WorldWorker(e.settings)
            results = list(worker._world_heartbeats(e.wid, 7200))
            assert 3 < len(results) < 30
            assert results[0].current_time == e.at + timedelta(seconds=1)
        assert get(e, jid)["status"] == "completed"
        with e.db.read() as c:
            legs = c.execute(
                "SELECT * FROM journey_legs WHERE journey_id=? ORDER BY stop_index", (jid,)
            ).fetchall()
            stops = c.execute(
                "SELECT * FROM journey_stops WHERE journey_id=? ORDER BY stop_index", (jid,)
            ).fetchall()
            assert len(legs) == 3 and all(leg["status"] == "arrived" for leg in legs)
            assert len(stops) == 3 and all(stop["arrived_at"] for stop in stops)
            for prior, later in zip(stops, legs[1:], strict=False):
                assert from_iso(later["started_at"]) >= from_iso(prior["arrived_at"]) + timedelta(
                    minutes=prior["minutes"]
                )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM world_heartbeats WHERE world_id=?", (e.wid,)
                ).fetchone()[0]
                < 30
            )
    finally:
        if worker:
            worker.engine.close()
