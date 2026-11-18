"""哥哥，分房休息与会合分别取证，不用同屋假设代替真实活动。"""

# ruff: noqa: F811
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_appointment_rooms import door, lease
from test_appointments import people
from test_journey_room_appointments import current_time, room_at_stop
from test_journeys import advance_to_arrival, npc_choice
from test_journeys import choose as journey_choice
from test_journeys import get as journey
from test_lived_world import town  # noqa: F401
from test_overnight_stays import stopped_long_journey

from world_engine.database import Database
from world_engine.families import FamilyService
from world_engine.interiors import InteriorService
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.separate_stays import (
    SEPARATE_STAY_SCHEMA,
    SeparateStayIntent,
    SeparateStayService,
)


def set_time(e, at):
    # 哥哥，服务级计时只改隔离时钟，绝不沿旧帮助函数顺手补满身体资源。
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))


def setup(e, **changes):
    with e.db.write() as c:
        c.executescript(SEPARATE_STAY_SCHEMA)
    jid = stopped_long_journey(e)
    own = room_at_stop(e, jid, owner=e.player, name="玩家独立客房")
    other = room_at_stop(e, jid, owner=e.npc, name="同伴独立客房")
    at = current_time(e)
    fields = dict(
        operation="propose",
        journey_id=jid,
        own_room_id=own,
        other_room_id=other,
        arrival_world_time=to_iso(at + timedelta(minutes=30)),
        window_minutes=60,
        minutes=480,
        reunion_window_minutes=60,
        statement="我想各住自己的客房，休息八小时后在原室外站点会合；房租与恢复旅行分别商量。",
    )
    fields.update(changes)
    return jid, own, other, fields


def choose(e, operation=None, sid=None, *, npc=False, **changes):
    fields = {
        "operation": operation,
        "statement": "这是本人对这份分房住宿和会合的明确选择。",
        **changes,
    }
    if sid:
        fields["stay_id"] = sid
    with e.db.write() as c:
        player, companion = people(e, c)
        return SeparateStayService.apply(
            c,
            companion if npc else player,
            player if npc else companion,
            current_time(e),
            SeparateStayIntent(**fields),
        )


def get(e, sid):
    with e.db.read() as c:
        row = dict(c.execute("SELECT * FROM journey_separate_stays WHERE id=?", (sid,)).fetchone())
        row["participants"] = {
            p["character_id"]: dict(p)
            for p in c.execute(
                "SELECT * FROM journey_separate_stay_participants WHERE stay_id=?", (sid,)
            )
        }
        return row


def accept(e, fields):
    result = choose(e, **fields)
    sid = result["stay_id"]
    choose(e, "accept", sid, npc=True)
    return sid


def npc_perform(e, at):
    set_time(e, at)
    with e.db.write() as c:
        _, npc = people(e, c)
        return SeparateStayService.perform(c, npc, at)


def enter_player(e, rid):
    for op in ("open", "enter"):
        result = door(e, rid, op)
        assert result.status_code == 200, result.text


def begin(e, fields, own, other, *, player_late=0):
    sid = accept(e, fields)
    start = from_iso(fields["arrival_world_time"])
    assert "入口" in npc_perform(e, start - timedelta(minutes=5))
    enter_player(e, own)
    assert "完整分房休息" in npc_perform(e, start)
    set_time(e, start + timedelta(minutes=player_late))
    choose(e, "check_in", sid)
    return sid, start


def settle(e, at):
    before = current_time(e)
    set_time(e, at)
    with e.db.write() as c:
        heartbeat = str(uuid4())
        stamp = to_iso(utc_now())
        c.execute(
            "INSERT INTO world_heartbeats(id,world_id,real_time,real_elapsed_seconds,"
            "time_scale,world_delta_seconds,world_time_before,world_time_after,created_at) "
            "VALUES (?,?,?,0,1,?,?,?,?)",
            (
                heartbeat,
                e.wid,
                stamp,
                max(0, (at - before).total_seconds()),
                to_iso(before),
                to_iso(at),
                stamp,
            ),
        )
        SeparateStayService.maintain(c, e.wid, at, complete=False)
        LifeActivityService.advance(c, e.wid, at, heartbeat)
        SeparateStayService.maintain(c, e.wid, at)


def test_each_person_enters_and_starts_own_rest_without_cross_room_permission(town):
    e = town
    jid, own, other, fields = setup(e)
    sid = accept(e, fields)
    assert journey(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE status='running'"
        ).fetchone()
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, other), e.player)
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, own), e.npc)
    start = from_iso(fields["arrival_world_time"])
    set_time(e, start)
    with pytest.raises(ValueError, match="实际进入"):
        choose(e, "check_in", sid)
    enter_player(e, own)
    choose(e, "check_in", sid)
    parts = get(e, sid)["participants"]
    assert parts[e.player]["status"] == "active" and parts[e.npc]["status"] == "waiting"
    with pytest.raises(ValueError, match="NPC"):
        choose(e, "check_in", sid, npc=True)
    assert "入口" in npc_perform(e, start)
    assert "完整分房休息" in npc_perform(e, start + timedelta(minutes=5))
    assert get(e, sid)["participants"][e.npc]["status"] == "active"


def test_staggered_full_rest_then_real_outdoor_reunion(town):
    e = town
    jid, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other, player_late=30)
    row = get(e, sid)
    assert from_iso(row["participants"][e.player]["ends_at"]) == start + timedelta(
        hours=8, minutes=30
    )
    assert from_iso(row["participants"][e.npc]["ends_at"]) == start + timedelta(hours=8)
    settle(e, start + timedelta(hours=8))
    assert get(e, sid)["participants"][e.npc]["status"] == "completed"
    assert get(e, sid)["participants"][e.player]["status"] == "active"
    assert npc_perform(e, start + timedelta(hours=8, minutes=5)) == "离开房间"
    settle(e, start + timedelta(hours=8, minutes=30))
    row = get(e, sid)
    assert row["status"] == "completed" and row["reunion_status"] == "waiting"
    assert row["participants"][e.player]["status"] == "completed"
    assert journey_choice(e, "resume", jid).status_code == 409
    assert door(e, own, "exit").status_code == 200
    reunion = from_iso(row["reunion_starts_at"])
    settle(e, reunion - timedelta(seconds=1))
    assert get(e, sid)["reunion_status"] == "waiting"
    settle(e, reunion)
    row = get(e, sid)
    assert row["reunion_status"] == "met"
    assert journey(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert not SeparateStayService.blocks_journey(c, jid, reunion)
        assert not c.execute("SELECT 1 FROM shared_life_moments").fetchone()
        event = c.execute(
            "SELECT summary FROM world_events WHERE id=?", (row["source_event_id"],)
        ).fetchone()[0]
        assert "分别" in event or "不同" in event


@pytest.mark.parametrize("minutes", [240, 720])
def test_each_duration_endpoint_requires_complete_real_activity(town, minutes):
    e = town
    _, own, other, fields = setup(e, minutes=minutes)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(minutes=minutes, seconds=-1))
    assert get(e, sid)["status"] == "active"
    settle(e, start + timedelta(minutes=minutes))
    assert get(e, sid)["status"] == "completed"
    assert all(p["status"] == "completed" for p in get(e, sid)["participants"].values())


@pytest.mark.parametrize(
    "changes",
    [
        {"own_room_id": "same", "other_room_id": "same"},
        {"allow_visit": True},
        {"minutes": 239},
        {"minutes": 721},
        {"reunion_window_minutes": 14},
        {"reunion_window_minutes": 121},
    ],
)
def test_invalid_or_cross_room_reception_fields_are_rejected(changes):
    with pytest.raises(ValidationError):
        SeparateStayIntent(
            **{
                "operation": "propose",
                "journey_id": "trip",
                "own_room_id": "a",
                "other_room_id": "b",
                "arrival_world_time": "2030-01-01T00:00:00Z",
                "statement": "本人明确选择分房住宿。",
                **changes,
            }
        )


def test_accept_cannot_rewrite_room_duration_or_reunion():
    for changed in (
        {"own_room_id": "a"},
        {"other_room_id": "b"},
        {"minutes": 240},
        {"window_minutes": 15},
        {"reunion_window_minutes": 15},
    ):
        with pytest.raises(ValidationError):
            SeparateStayIntent(
                operation="accept", stay_id="real", statement="我只接受已记录的安排。", **changed
            )


def test_decline_or_cancel_does_not_move_pay_or_give_cross_access(town):
    e = town
    jid, own, other, fields = setup(e)
    with e.db.read() as c:
        before = {p["id"]: (p["money"], p["current_room_id"]) for p in people(e, c)}
    sid = choose(e, **fields)["stay_id"]
    choose(e, "decline", sid, npc=True)
    assert get(e, sid)["status"] == "declined"
    assert journey(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert {p["id"]: (p["money"], p["current_room_id"]) for p in people(e, c)} == before
        assert not c.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging'").fetchone()
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, own), e.npc)
        assert not InteriorService.allowed(c, InteriorService.room(c, e.wid, other), e.player)


def test_wrong_person_cannot_accept_own_proposal_or_withdraw_received_pending(town):
    e = town
    _, _, _, fields = setup(e)
    sid = choose(e, **fields)["stay_id"]
    with pytest.raises(ValueError, match="收到"):
        choose(e, "accept", sid)
    with pytest.raises(ValueError, match="撤回"):
        choose(e, "cancel", sid, npc=True)
    assert get(e, sid)["status"] == "pending"


def test_deadline_must_include_post_sleep_reunion_window(town):
    e = town
    jid, _, _, fields = setup(e)
    start = from_iso(fields["arrival_world_time"])
    with e.db.write() as c:
        c.execute(
            "UPDATE joint_journeys SET deadline_at=? WHERE id=?",
            (to_iso(start + timedelta(hours=9, minutes=30)), jid),
        )
    with pytest.raises(ValueError, match="期限"):
        choose(e, **fields)
    assert journey(e, jid)["status"] == "active"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_separate_stays").fetchone()


def test_no_unknown_other_private_rooms_are_disclosed(town):
    e = town
    jid, _, _, fields = setup(e)
    hidden = room_at_stop(e, jid, owner=e.npc, name="门后秘密内室", parent=fields["other_room_id"])
    with e.db.read() as c:
        options = SeparateStayService.room_options(c, e.wid, e.player, e.npc, current_time(e))
        assert hidden not in {r["id"] for r in options}
    fields["other_room_id"] = hidden
    with pytest.raises(ValueError, match="已认识"):
        choose(e, **fields)


def test_individual_missed_window_preserves_other_running_rest(town):
    e = town
    _, own, _, fields = setup(e)
    sid = accept(e, fields)
    start = from_iso(fields["arrival_world_time"])
    set_time(e, start)
    enter_player(e, own)
    choose(e, "check_in", sid)
    settle(e, start + timedelta(minutes=60))
    assert get(e, sid)["participants"][e.npc]["status"] == "waiting"
    settle(e, start + timedelta(minutes=60, seconds=1))
    row = get(e, sid)
    assert row["participants"][e.npc]["status"] == "missed"
    assert (
        row["participants"][e.player]["status"] == "active" and row["reunion_status"] == "cancelled"
    )
    settle(e, start + timedelta(hours=8))
    row = get(e, sid)
    assert row["participants"][e.player]["status"] == "completed" and row["status"] == "interrupted"


def test_sickness_before_settlement_does_not_award_full_recovery(town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    with e.db.write() as c:
        c.execute("UPDATE characters SET health=health-10 WHERE id=?", (e.player,))
        initial_energy = c.execute(
            "SELECT energy FROM characters WHERE id=?", (e.player,)
        ).fetchone()[0]
    settle(e, start + timedelta(hours=8))
    row = get(e, sid)
    assert row["participants"][e.player]["status"] == "interrupted"
    assert row["participants"][e.npc]["status"] == "completed"
    with e.db.read() as c:
        activity = c.execute(
            "SELECT status FROM character_life_activities WHERE id=?",
            (row["participants"][e.player]["activity_id"],),
        ).fetchone()
        assert activity["status"] == "interrupted"
        assert (
            c.execute("SELECT energy FROM characters WHERE id=?", (e.player,)).fetchone()[0]
            == initial_energy
        )


def test_cancel_only_stops_bound_running_activities_and_preserves_completed(town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other, player_late=30)
    settle(e, start + timedelta(hours=8))
    with e.db.write() as c:
        _, npc = people(e, c)
        independent = LifeActivityService.start(c, npc, start + timedelta(hours=8), "wait", 30)
    choose(e, "cancel", sid)
    row = get(e, sid)
    assert row["participants"][e.npc]["status"] == "completed"
    assert row["participants"][e.player]["status"] == "interrupted"
    assert row["reunion_status"] == "cancelled"
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM character_life_activities WHERE id=?", (independent,)
            ).fetchone()[0]
            == "running"
        )


def test_reunion_window_ends_inclusively_and_does_not_auto_resume(town):
    e = town
    jid, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(hours=8))
    row = get(e, sid)
    assert door(e, own, "exit").status_code == 200
    npc_perform(e, start + timedelta(hours=8, minutes=5))
    settle(e, from_iso(row["reunion_ends_at"]))
    assert get(e, sid)["reunion_status"] == "met"
    assert journey(e, jid)["status"] == "paused"


def test_missing_reunion_keeps_completed_sleep_and_drops_commitment(town):
    e = town
    jid, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(hours=8))
    row = get(e, sid)
    settle(e, from_iso(row["reunion_ends_at"]) + timedelta(seconds=1))
    row = get(e, sid)
    assert row["status"] == "completed" and row["reunion_status"] == "missed"
    assert all(p["status"] == "completed" for p in row["participants"].values())
    with e.db.read() as c:
        assert not SeparateStayService.blocks_journey(
            c, jid, from_iso(row["reunion_ends_at"]) + timedelta(seconds=1)
        )


def test_lease_must_cover_latest_checkin_plus_entire_rest(town):
    e = town
    jid, _, _, fields = setup(e)
    own = room_at_stop(e, jid, owner=e.npc, name="真实付费客房", rate=1)
    fields["own_room_id"] = own
    # 哥哥，真实合同由既有测试帮助函数建立，分房服务本身绝不补租约。
    lease(e, own, e.player)
    with e.db.write() as c:
        c.execute(
            "UPDATE contract_fulfillments SET due_world_time=? WHERE asset_id=? AND kind='lodging'",
            (
                to_iso(from_iso(fields["arrival_world_time"]) + timedelta(hours=8, minutes=59)),
                own,
            ),
        )
    with pytest.raises(ValueError, match="租约"):
        choose(e, **fields)


def test_rows_hide_companion_private_activity_details(town):
    e = town
    _, own, other, fields = setup(e)
    sid, _ = begin(e, fields, own, other)
    with e.db.read() as c:
        rows = SeparateStayService.rows(c, e.wid, e.player, current_time(e), e.npc)
        row = next(r for r in rows if r["id"] == sid)
        companion = next(p for p in row["participants"] if p["character_id"] == e.npc)
        player = next(p for p in row["participants"] if p["character_id"] == e.player)
        assert companion["room_name"] and companion["location_name"]
        assert companion["status"] == "unknown"
        assert (
            "activity_id" not in companion
            and "checked_in_at" not in companion
            and "reason" not in companion
        )
        assert player["activity_id"] and player["checked_in_at"]


def test_independent_private_events_and_full_completion_are_unknown_until_reunion(town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(hours=8))
    row = get(e, sid)
    assert row["status"] == "completed"
    with e.db.read() as c:
        public = SeparateStayService.rows(c, e.wid, e.player, current_time(e), e.npc)[0]
        assert public["status"] == "accepted"
        assert (
            next(p for p in public["participants"] if p["character_id"] == e.npc)["status"]
            == "unknown"
        )
        mine = next(p for p in public["participants"] if p["character_id"] == e.player)
        assert mine["status"] == "completed"
        event = row["participants"][e.npc]["source_event_id"]
        assert not c.execute(
            "SELECT 1 FROM event_observers WHERE event_id=? AND character_id=?", (event, e.player)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?",
            (event, e.player),
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM character_memories WHERE event_id=? AND character_id=?",
            (event, e.player),
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM player_notifications WHERE event_id=?", (event,)
        ).fetchone()
        assert public["source_event_id"] != event


def test_partial_body_failure_shares_only_generic_cancelled_reunion(town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=5 WHERE id=?", (e.npc,))
    settle(e, start + timedelta(minutes=15))
    with e.db.read() as c:
        value = SeparateStayService.rows(c, e.wid, e.player, current_time(e), e.npc)[0]
        assert value["status"] == "active" and value["reunion_status"] == "cancelled", get(e, sid)
        partner = next(p for p in value["participants"] if p["character_id"] == e.npc)
        assert partner["status"] == "unknown" and "reason" not in partner
        event = c.execute(
            "SELECT summary FROM world_events WHERE id=?", (value["reunion_event_id"],)
        ).fetchone()[0]
        assert "用饭" not in event and "饥饿" not in event


def test_room_hierarchy_cannot_route_one_bedroom_through_the_other(town):
    e = town
    jid, own, _, fields = setup(e)
    nested = room_at_stop(e, jid, owner=e.npc, name="另一人的内室", parent=own)
    with e.db.write() as c:
        c.execute(
            "INSERT OR IGNORE INTO life_room_access(room_id,character_id) VALUES (?,?)",
            (own, e.npc),
        )
        c.execute(
            "INSERT OR IGNORE INTO life_room_access(room_id,character_id) VALUES (?,?)",
            (nested, e.player),
        )
    fields["other_room_id"] = nested
    with pytest.raises(ValueError, match="内外通路"):
        choose(e, **fields)


def test_window_endpoint_allows_only_one_persons_full_late_rest(town):
    e = town
    _, own, _, fields = setup(e)
    sid = accept(e, fields)
    endpoint = from_iso(fields["arrival_world_time"]) + timedelta(minutes=fields["window_minutes"])
    set_time(e, endpoint)
    enter_player(e, own)
    choose(e, "check_in", sid)
    part = get(e, sid)["participants"][e.player]
    assert from_iso(part["ends_at"]) == endpoint + timedelta(minutes=fields["minutes"])
    with pytest.raises(ValueError):
        choose(e, "check_in", sid)


def test_actual_clock_segments_npc_arrival_own_start_and_entire_rest(town):
    e = town
    _, own, _, fields = setup(e)
    sid = accept(e, fields)
    start = from_iso(fields["arrival_world_time"])
    e.clock.heartbeat(e.wid, elapsed_seconds=(start - current_time(e)).total_seconds())
    enter_player(e, own)
    choose(e, "check_in", sid)
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    row = get(e, sid)
    assert row["status"] == "completed"
    assert all(p["status"] == "completed" for p in row["participants"].values())
    with e.db.read() as c:
        npc = row["participants"][e.npc]
        assert from_iso(npc["ends_at"]) - from_iso(npc["checked_in_at"]) == timedelta(hours=8)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_heartbeats WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            > 2
        )


def test_met_reunion_keeps_window_wait_but_can_be_explicitly_released(town):
    e = town
    jid, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(hours=8))
    assert npc_perform(e, start + timedelta(hours=8, minutes=5)) == "离开房间"
    assert door(e, own, "exit").status_code == 200
    reunion = from_iso(get(e, sid)["reunion_starts_at"])
    settle(e, reunion)
    assert get(e, sid)["reunion_status"] == "met"
    with e.db.read() as c:
        _, npc = people(e, c)
        assert SeparateStayService.planned_minutes(c, npc, reunion) == 0
        assert not SeparateStayService.blocks_journey(c, jid, reunion)
        assert SeparateStayService.rows(c, e.wid, e.player, reunion, e.npc)[0][
            "can_end_reunion_wait"
        ]
    assert "等候" in npc_perform(e, reunion + timedelta(minutes=5))
    choose(e, "cancel", sid)
    row = get(e, sid)
    assert row["status"] == "completed" and row["reunion_status"] == "met"
    assert row["reunion_wait_ended_at"]
    with e.db.read() as c:
        _, npc = people(e, c)
        assert SeparateStayService.planned_minutes(c, npc, current_time(e)) is None
    assert npc_perform(e, reunion + timedelta(minutes=10)) is None


def test_restart_keeps_distinct_bound_activities_and_actual_start_times(town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other, player_late=20)
    before = get(e, sid)
    restarted = Database(e.settings.database_path)
    restarted.initialize()
    with restarted.read() as c:
        parts = SeparateStayService._parts(c, sid)
        assert {p["activity_id"] for p in parts} == {
            p["activity_id"] for p in before["participants"].values()
        }
        assert len({p["room_id"] for p in parts}) == 2
    settle(e, start + timedelta(hours=8, minutes=20))
    assert get(e, sid)["status"] == "completed"


def test_new_care_need_interrupts_only_the_responsible_person(monkeypatch, town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    original = FamilyService.hungry_dependent

    def need(cls, c, person, at):
        return (
            {"id": "existing-dependent-needs-care"}
            if person["id"] == e.npc
            else original(c, person, at)
        )

    monkeypatch.setattr(FamilyService, "hungry_dependent", classmethod(need))
    settle(e, start + timedelta(minutes=15))
    parts = get(e, sid)["participants"]
    assert parts[e.npc]["status"] == "interrupted" and parts[e.player]["status"] == "active"
    settle(e, start + timedelta(hours=8))
    assert get(e, sid)["participants"][e.player]["status"] == "completed"


def test_delayed_clock_stops_only_hungry_bound_rest_at_real_threshold(town):
    e = town
    _, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=6 WHERE id=?", (e.npc,))
        c.execute("DELETE FROM character_state_accumulators WHERE character_id=?", (e.npc,))
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    row = get(e, sid)
    assert row["participants"][e.player]["status"] == "completed"
    assert row["participants"][e.npc]["status"] == "interrupted"
    expected = start + timedelta(hours=1 / e.settings.satiety_loss_per_world_hour)
    assert (
        abs((from_iso(row["participants"][e.npc]["finished_at"]) - expected).total_seconds())
        < 0.001
    )


def test_accepted_split_stays_survive_end_of_original_journey(town):
    e = town
    jid, own, other, fields = setup(e)
    sid = accept(e, fields)
    assert journey_choice(e, "end", jid).status_code == 200
    assert get(e, sid)["status"] == "accepted"
    start = from_iso(fields["arrival_world_time"])
    npc_perform(e, start - timedelta(minutes=5))
    enter_player(e, own)
    npc_perform(e, start)
    choose(e, "check_in", sid)
    settle(e, start + timedelta(hours=8))
    assert get(e, sid)["status"] == "completed"
    assert journey(e, jid)["status"] == "ended"


def test_pending_split_stay_is_cancelled_when_original_journey_ends(town):
    e = town
    jid, _, _, fields = setup(e)
    sid = choose(e, **fields)["stay_id"]
    assert journey_choice(e, "end", jid).status_code == 200
    assert get(e, sid)["status"] == "cancelled"


def test_new_appointment_conflicts_with_room_exit_and_reunion_window(town):
    e = town
    _, _, _, fields = setup(e)
    sid = accept(e, fields)
    row = get(e, sid)
    with e.db.read() as c:
        player, _ = people(e, c)
        with pytest.raises(ValueError, match="会合"):
            SeparateStayService.assert_interval_free(
                c,
                player,
                from_iso(row["latest_end_at"]),
                from_iso(row["reunion_ends_at"]),
                row["location_id"],
            )


def test_exact_latest_lease_end_permits_complete_sleep_and_real_checkout(town):
    e = town
    jid, _, other, fields = setup(e)
    own = room_at_stop(e, jid, owner=e.npc, name="租期恰好覆盖的客房", rate=1)
    fields["own_room_id"] = own
    lease(e, own, e.player)
    end = from_iso(fields["arrival_world_time"]) + timedelta(
        minutes=fields["window_minutes"] + fields["minutes"]
    )
    with e.db.write() as c:
        c.execute(
            "UPDATE contract_fulfillments SET due_world_time=? WHERE asset_id=? AND kind='lodging'",
            (to_iso(end), own),
        )
    sid, _ = begin(e, fields, own, other, player_late=fields["window_minutes"])
    settle(e, end)
    assert get(e, sid)["participants"][e.player]["status"] == "completed"
    assert door(e, own, "exit").status_code == 200


def test_both_resume_and_real_return_end_old_reunion_wait_permanently(town):
    e = town
    jid, own, other, fields = setup(e)
    sid, start = begin(e, fields, own, other)
    settle(e, start + timedelta(hours=8))
    npc_perform(e, start + timedelta(hours=8, minutes=5))
    assert door(e, own, "exit").status_code == 200
    reunion = from_iso(get(e, sid)["reunion_starts_at"])
    settle(e, reunion)
    assert get(e, sid)["reunion_status"] == "met"
    response = journey_choice(e, "resume", jid)
    assert response.status_code == 200, response.text
    assert not get(e, sid)["reunion_wait_ended_at"]
    npc_choice(e, jid, "resume")
    row = get(e, sid)
    assert row["reunion_wait_ended_at"] and row["reunion_status"] == "met"
    assert journey(e, jid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=120)
    response = journey_choice(e, "depart", jid)
    assert response.status_code == 200, response.text
    assert advance_to_arrival(e, jid)["status"] == "completed"
    assert current_time(e) < from_iso(row["reunion_ends_at"])
    with e.db.write() as c:
        _, npc = people(e, c)
        before = c.execute(
            "SELECT COUNT(*) FROM character_movements WHERE character_id=?", (e.npc,)
        ).fetchone()[0]
        assert SeparateStayService.perform(c, npc, current_time(e)) is None
        assert SeparateStayService.planned_minutes(c, npc, current_time(e)) is None
        assert (
            c.execute(
                "SELECT COUNT(*) FROM character_movements WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == before
        )
