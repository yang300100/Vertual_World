"""哥哥，整夜住宿分别确认时间、房间与接待，只有真实入住才开始恢复。"""

# ruff: noqa: F811
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_appointment_rooms import daily, door, lease, room
from test_appointments import choose as appointment_choice
from test_appointments import people, set_time
from test_journey_room_appointments import current_time, room_at_stop
from test_journeys import advance_to_arrival, npc_choice, offer
from test_journeys import choose as journey_choice
from test_journeys import get as journey
from test_lived_world import town  # noqa: F401

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.families import FamilyService
from world_engine.interiors import InteriorService
from world_engine.life import LifeActivityService
from world_engine.overnight import OvernightIntent, OvernightService
from world_engine.repository import from_iso, to_iso, utc_now


def stopped_long_journey(e):
    # 哥哥，整夜所需期限从首次提议就明确，不偷偷延长原来的四小时旅行。
    jid = offer(e, deadline_world_time=to_iso(e.at + timedelta(days=2)))
    assert journey_choice(e, "accept", jid).status_code == 200
    assert journey_choice(e, "depart", jid).status_code == 200
    assert advance_to_arrival(e, jid)["phase"] == "stopped"
    return jid


def get(e, sid):
    with e.db.read() as c:
        return dict(
            c.execute("SELECT * FROM journey_overnight_stays WHERE id=?", (sid,)).fetchone()
        )


def terms(e, jid, rid, **changes):
    return {
        "operation": "propose",
        "journey_id": jid,
        "room_id": rid,
        "arrival_world_time": to_iso(current_time(e) + timedelta(minutes=30)),
        "window_minutes": 60,
        "minutes": 480,
        "allow_visit": True,
        "statement": "暂停旅行，邀请在指定房间休息八小时，接待许可按本次明确选择，房租另议。",
        **changes,
    }


def remember_terms(e, sid, fields):
    if not hasattr(e, "overnight_terms"):
        e.overnight_terms = {}
    e.overnight_terms[sid] = fields


def npc_offer(e, jid, rid, **changes):
    fields = terms(e, jid, rid, **changes)
    with e.db.write() as c:
        player, npc = people(e, c)
        result = OvernightService.apply(c, npc, player, current_time(e), OvernightIntent(**fields))
    sid = result["stay_id"]
    remember_terms(e, sid, fields)
    return sid


def choose(e, operation, sid=None, **changes):
    statement = changes.pop(
        "statement", "我明确表达本人对这份整夜住宿的具体选择，进屋仍由本人完成。"
    )
    choice = {"operation": operation, "statement": statement, **changes}
    if sid:
        choice["stay_id"] = sid
    body = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": statement,
        "evidence": statement,
        "overnight_choice": choice,
    }
    response = e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body)
    return response, body


def npc_respond(e, sid, operation="accept", **changes):
    with e.db.write() as c:
        player, npc = people(e, c)
        return OvernightService.apply(
            c,
            npc,
            player,
            current_time(e),
            OvernightIntent(
                operation=operation,
                stay_id=sid,
                statement="这是我本人对这份整夜住宿的独立选择。",
                **changes,
            ),
        )


def activities(e, *, running=False):
    with e.db.read() as c:
        return [
            dict(row)
            for row in c.execute(
                "SELECT * FROM character_life_activities WHERE world_id=? AND character_id IN (?,?)"
                + (" AND status='running'" if running else "")
                + " ORDER BY character_id,id",
                (e.wid, e.player, e.npc),
            )
        ]


def accepted_stay(e, jid, rid, **changes):
    sid = npc_offer(e, jid, rid, **changes)
    response, _ = choose(e, "accept", sid)
    assert response.status_code == 200, response.text
    return sid


def enter_npc_hosted_room(e, sid, rid, *, late_minutes=0):
    at = from_iso(e.overnight_terms[sid]["arrival_world_time"]) + timedelta(minutes=late_minutes)
    daily(e, at)
    with e.db.read() as c:
        player, npc = people(e, c)
        assert npc["current_room_id"] == rid and player["current_room_id"] is None
    response = door(e, rid, "enter")
    assert response.status_code == 200, response.text
    return at


def begin(e, jid, rid, *, late_minutes=0, **changes):
    sid = accepted_stay(e, jid, rid, **changes)
    at = enter_npc_hosted_room(e, sid, rid, late_minutes=late_minutes)
    response, body = choose(e, "check_in", sid)
    assert response.status_code == 200, response.text
    return sid, at, body


def test_eight_hours_need_separate_acceptance_real_entry_and_player_check_in(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid = npc_offer(e, jid, rid)
    assert get(e, sid)["status"] == "pending"
    assert journey(e, jid)["status"] == "paused"
    assert not activities(e, running=True)
    with pytest.raises(ValueError):
        npc_respond(e, sid)
    assert choose(e, "check_in", sid)[0].status_code == 409
    assert choose(e, "accept", sid)[0].status_code == 200
    with e.db.read() as c:
        assert all(p["current_room_id"] is None for p in people(e, c))
    at = enter_npc_hosted_room(e, sid, rid)
    assert get(e, sid)["status"] == "accepted" and not activities(e, running=True)
    assert choose(e, "check_in", sid)[0].status_code == 200
    running = activities(e, running=True)
    assert len(running) == 2 and all(a["kind"] == "rest" for a in running)
    assert all(from_iso(a["started_world_time"]) == at for a in running)
    assert all(from_iso(a["ends_world_time"]) == at + timedelta(hours=8) for a in running)
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM shared_life_moments").fetchone()
        assert not c.execute("SELECT 1 FROM social_appointments").fetchone()
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600 - 1)
    assert get(e, sid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, sid)["status"] == "completed"
    assert journey(e, jid)["status"] == "paused"


def test_late_arrival_still_requires_full_eight_hours_from_actual_check_in(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, at, _ = begin(e, jid, rid, late_minutes=45)
    assert all(
        from_iso(a["ends_world_time"]) == at + timedelta(hours=8)
        for a in activities(e, running=True)
    )
    e.clock.heartbeat(e.wid, elapsed_seconds=7 * 3600 + 59 * 60)
    assert get(e, sid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    assert get(e, sid)["status"] == "completed"


@pytest.mark.parametrize("minutes", [240, 720])
def test_minimum_and_maximum_stays_require_the_entire_real_duration(town, minutes):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, started, _ = begin(e, jid, rid, minutes=minutes)
    # 哥哥，四小时和十二小时都必须真正度过，不能被旧短休上限截断或提前完成。
    e.clock.heartbeat(e.wid, elapsed_seconds=minutes * 60 - 1)
    assert get(e, sid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, sid)["status"] == "completed"
    row = get(e, sid)
    with e.db.read() as c:
        bound = c.execute(
            "SELECT * FROM character_life_activities WHERE id IN (?,?)",
            (row["proposer_activity_id"], row["recipient_activity_id"]),
        ).fetchall()
    assert len(bound) == 2 and all(a["status"] == "completed" for a in bound)
    assert all(
        from_iso(a["finished_world_time"]) == started + timedelta(minutes=minutes)
        for a in bound
    )


def test_crossing_midnight_does_not_complete_or_reset_the_stay(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    arrival = e.at.replace(hour=22, minute=0, second=0, microsecond=0)
    sid, at, _ = begin(e, jid, rid, arrival_world_time=to_iso(arrival))
    e.clock.heartbeat(e.wid, elapsed_seconds=2 * 3600)
    assert current_time(e).date() == (at + timedelta(days=1)).date()
    assert get(e, sid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=6 * 3600)
    assert get(e, sid)["status"] == "completed"
    assert current_time(e).hour == 6


def test_proposal_acceptance_and_check_in_do_not_pay_rent_or_grant_storage(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid, rate=9)
    with e.db.read() as c:
        money = {p["id"]: p["money"] for p in people(e, c)}
    sid, _, _ = begin(e, jid, rid)
    with e.db.read() as c:
        target = InteriorService.room(c, e.wid, rid)
        assert {p["id"]: p["money"] for p in people(e, c)} == money
        assert not c.execute("SELECT 1 FROM contract_fulfillments WHERE kind='lodging'").fetchone()
        assert not c.execute(
            "SELECT 1 FROM life_room_access WHERE room_id=? AND character_id=?", (rid, e.player)
        ).fetchone()
        assert InteriorService.allowed(c, target, e.player)
        assert not InteriorService.keyed(c, target, e.player)
        assert not InteriorService.storage_allowed(c, target, e.player)
    assert get(e, sid)["status"] == "active"


def test_accepting_rest_does_not_implicitly_authorize_player_owned_room(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid, owner=e.player)
    sid = npc_offer(e, jid, rid, allow_visit=False)
    assert choose(e, "accept", sid)[0].status_code == 409
    assert get(e, sid)["status"] == "pending"
    assert choose(e, "accept", sid, allow_visit=True)[0].status_code == 200
    daily(e, from_iso(e.overnight_terms[sid]["arrival_world_time"]))
    with e.db.read() as c:
        assert all(p["current_room_id"] is None for p in people(e, c))
    assert choose(e, "check_in", sid)[0].status_code == 409
    assert not activities(e, running=True)


def test_nested_room_route_does_not_skip_doors_or_move_player(town):
    e = town
    jid = stopped_long_journey(e)
    outer = room_at_stop(e, jid, name="整夜住宿前厅")
    inner = room_at_stop(e, jid, parent=outer, name="整夜住宿内室")
    sid = accepted_stay(e, jid, inner)
    at = from_iso(e.overnight_terms[sid]["arrival_world_time"])
    daily(e, at - timedelta(minutes=15))
    with e.db.read() as c:
        player, npc = people(e, c)
        assert player["current_room_id"] is None and npc["current_room_id"] == outer
    daily(e, at - timedelta(minutes=10))
    with e.db.read() as c:
        assert people(e, c)[1]["current_room_id"] == inner
    set_time(e, at)
    assert door(e, inner, "enter").status_code == 409
    assert choose(e, "check_in", sid)[0].status_code == 409
    assert door(e, outer, "enter").status_code == 200
    assert choose(e, "check_in", sid)[0].status_code == 409
    assert door(e, inner, "enter").status_code == 200
    assert choose(e, "check_in", sid)[0].status_code == 200


@pytest.mark.parametrize("obstacle", ["wrong_stop", "third_party_room", "third_party_lease"])
def test_unavailable_rooms_roll_back_proposal_and_journey_pause(town, obstacle):
    e = town
    jid = stopped_long_journey(e)
    with e.db.read() as c:
        third = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    if obstacle == "wrong_stop":
        rid = room(e)
    elif obstacle == "third_party_room":
        rid = room_at_stop(e, jid, owner=third)
    else:
        rid = room_at_stop(e, jid, rate=1)
        lease(SimpleNamespace(**{**vars(e), "at": current_time(e)}), rid, third)
    before = journey(e, jid)
    with pytest.raises(ValueError):
        npc_offer(e, jid, rid)
    assert journey(e, jid)["status"] == "active"
    assert journey(e, jid)["source_event_id"] == before["source_event_id"]
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_overnight_stays").fetchone()


def test_original_deadline_must_cover_latest_check_in_plus_entire_sleep(town):
    e = town
    jid = offer(e)
    assert journey_choice(e, "accept", jid).status_code == 200
    assert journey_choice(e, "depart", jid).status_code == 200
    advance_to_arrival(e, jid)
    rid = room_at_stop(e, jid)
    with pytest.raises(ValueError):
        npc_offer(e, jid, rid)
    assert journey(e, jid)["status"] == "active"


@pytest.mark.parametrize("phase", ["pending", "accepted", "moving"])
def test_overnight_cannot_replace_real_arrival_or_interrupt_an_unfinished_leg(town, phase):
    e = town
    jid = offer(e, deadline_world_time=to_iso(e.at + timedelta(days=2)))
    rid = room_at_stop(e, jid)
    if phase != "pending":
        assert journey_choice(e, "accept", jid).status_code == 200
    if phase == "moving":
        assert journey_choice(e, "depart", jid).status_code == 200
    before = journey(e, jid)
    with pytest.raises(ValueError):
        npc_offer(e, jid, rid)
    after = journey(e, jid)
    assert after["status"] == before["status"]
    assert after["source_event_id"] == before["source_event_id"]
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_overnight_stays").fetchone()
        if phase == "moving":
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM character_movements WHERE status='moving' "
                    "AND character_id IN (?,?)",
                    (e.player, e.npc),
                ).fetchone()[0]
                == 2
            )


@pytest.mark.parametrize("status", ["pending", "accepted", "active"])
def test_linked_stay_blocks_both_sides_resuming_the_original_journey(town, status):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    if status == "active":
        sid, _, _ = begin(e, jid, rid)
    elif status == "accepted":
        sid = accepted_stay(e, jid, rid)
    else:
        sid = npc_offer(e, jid, rid)
    assert get(e, sid)["status"] == status
    assert journey_choice(e, "resume", jid).status_code == 409
    with pytest.raises(ValueError):
        npc_choice(e, jid, "resume")
    assert journey(e, jid)["status"] == "paused"


def test_unconfirmed_stay_is_cancelled_when_original_journey_ends(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid = npc_offer(e, jid, rid)
    assert journey_choice(e, "end", jid).status_code == 200
    assert get(e, sid)["status"] == "cancelled"
    assert choose(e, "accept", sid)[0].status_code == 409
    assert not activities(e, running=True)


@pytest.mark.parametrize("status", ["accepted", "active"])
def test_confirmed_stay_continues_independently_after_original_journey_ends(town, status):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    if status == "active":
        sid, _, _ = begin(e, jid, rid)
    else:
        sid = accepted_stay(e, jid, rid)
    assert journey_choice(e, "end", jid).status_code == 200
    assert get(e, sid)["status"] == status
    if status == "accepted":
        enter_npc_hosted_room(e, sid, rid)
        assert choose(e, "check_in", sid)[0].status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    assert get(e, sid)["status"] == "completed"
    assert journey(e, jid)["status"] == "ended"


def test_arrival_window_expiry_does_not_start_a_late_sleep(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid = accepted_stay(e, jid, rid, window_minutes=15)
    at = from_iso(e.overnight_terms[sid]["arrival_world_time"]) + timedelta(minutes=16)
    set_time(e, at)
    with e.db.write() as c:
        OvernightService.maintain(c, e.wid, at)
    assert get(e, sid)["status"] == "missed"
    assert choose(e, "check_in", sid)[0].status_code == 409
    assert not activities(e, running=True)


def test_active_stay_interrupts_when_hunger_needs_attention(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, _, _ = begin(e, jid, rid)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=5 WHERE id=?", (e.npc,))
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    assert get(e, sid)["status"] == "interrupted"
    assert not activities(e, running=True)
    assert journey(e, jid)["status"] == "paused"


def test_new_childcare_need_interrupts_the_bound_rest(monkeypatch, town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, _, _ = begin(e, jid, rid)
    original = FamilyService.hungry_dependent

    def new_need(cls, c, parent, at):
        # 哥哥，此处只隔离新出现的照护需求信号，不伪造孩子或已经喂食的事实。
        return (
            {"id": "existing-dependent-needs-care"}
            if parent["id"] == e.npc
            else original(c, parent, at)
        )

    monkeypatch.setattr(FamilyService, "hungry_dependent", classmethod(new_need))
    at = current_time(e)
    with e.db.write() as c:
        OvernightService.maintain(c, e.wid, at)
    assert get(e, sid)["status"] == "interrupted"
    assert not activities(e, running=True)


def test_permissions_changing_before_final_heartbeat_interrupt_instead_of_complete(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid, rate=1)
    sid, _, _ = begin(e, jid, rid)
    with e.db.read() as c:
        third = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
    lease(SimpleNamespace(**{**vars(e), "at": current_time(e)}), rid, third)
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    assert get(e, sid)["status"] == "interrupted"


@pytest.mark.parametrize("participant", ["player", "npc"])
@pytest.mark.parametrize("change", ["health", "longitude"])
def test_body_or_position_change_stops_both_before_rest_recovery(town, participant, change):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, _, _ = begin(e, jid, rid)
    with e.db.write() as c:
        cid = getattr(e, participant)
        c.execute(
            "UPDATE characters SET health=health-1 WHERE id=?"
            if change == "health"
            else "UPDATE characters SET longitude=longitude+0.01 WHERE id=?",
            (cid,),
        )
    # 哥哥，直接跨到结束也不能先让另一人拿到八小时休息收益。
    result = e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    assert get(e, sid)["status"] == "interrupted"
    assert not activities(e, running=True)
    assert all(a["energy_steps"] == 0 for a in activities(e))
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_state_updates WHERE heartbeat_id=? AND cause='rest_time_passage' "
            "AND character_id IN (?,?)",
            (result.heartbeat_id, e.player, e.npc),
        ).fetchone()


@pytest.mark.parametrize("entry", ["heartbeat", "clock_change", "api_heartbeat", "api_clock"])
def test_delayed_public_clock_entries_settle_hunger_before_eight_hour_completion(town, entry):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, start, _ = begin(e, jid, rid)
    # 哥哥，隔离存档模拟睡后需要用饭，既有活动仍须在真实阈值处中断。
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=6 WHERE id=?", (e.npc,))
        c.execute("DELETE FROM character_state_accumulators WHERE character_id=?", (e.npc,))
        c.execute(
            "UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?",
            (to_iso(utc_now() - timedelta(hours=8)), e.wid),
        )
    if entry == "heartbeat":
        result = e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
        assert result.current_time - result.previous_time == timedelta(hours=8)
    elif entry == "clock_change":
        result = e.clock.set_time_scale(e.wid, 2)
        assert result.settled_world_seconds >= 8 * 3600
    else:
        result = (
            e.client.post(f"/api/worlds/{e.wid}/heartbeat")
            if entry == "api_heartbeat"
            else e.client.patch(
                f"/api/worlds/{e.wid}/clock",
                json={"time_scale": 2},
            )
        )
        assert result.status_code == 200, result.text
    row = get(e, sid)
    assert row["status"] == "interrupted"
    with e.db.read() as c:
        acts = c.execute(
            "SELECT * FROM character_life_activities WHERE id IN (?,?)",
            (row["proposer_activity_id"], row["recipient_activity_id"]),
        ).fetchall()
        expected = start + timedelta(hours=1 / e.settings.satiety_loss_per_world_hour)
        assert all(
            abs((from_iso(a["finished_world_time"]) - expected).total_seconds()) < 0.001
            for a in acts
        )
        assert all(a["energy_steps"] < 8 * 5 for a in acts)


def test_overnight_uses_the_running_clock_hunger_rate_including_fractional_residual(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, start, _ = begin(e, jid, rid)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=31 WHERE id=?", (e.npc,))
        c.execute(
            "INSERT INTO character_state_accumulators(character_id,world_id,satiety_residual,energy_residual,updated_at) "
            "VALUES (?,?,0.5,0,?) ON CONFLICT(character_id) DO UPDATE SET satiety_residual=0.5",
            (e.npc, e.wid, to_iso(utc_now())),
        )
    e.clock.settings = replace(e.settings, satiety_loss_per_world_hour=12)
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    row = get(e, sid)
    assert row["status"] == "interrupted"
    with e.db.read() as c:
        ended = c.execute(
            "SELECT finished_world_time FROM character_life_activities WHERE id=?",
            (row["recipient_activity_id"],),
        ).fetchone()[0]
    assert from_iso(ended) == start + timedelta(hours=25.5 / 12)


def test_sleep_can_complete_exactly_when_existing_room_lease_ends(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid, rate=1)
    now = current_time(e)
    lease(SimpleNamespace(**{**vars(e), "at": now}), rid, e.player)
    with e.db.read() as c:
        due = from_iso(
            c.execute(
                "SELECT due_world_time FROM contract_fulfillments "
                "WHERE asset_id=? AND kind='lodging' AND status='active'",
                (rid,),
            ).fetchone()[0]
        )
    # 哥哥，到场窗末包含在内，最晚入住后八小时恰好结束于已有租约的截止。
    arrival = due - timedelta(hours=9)
    sid = npc_offer(e, jid, rid, allow_visit=False, arrival_world_time=to_iso(arrival))
    assert choose(e, "accept", sid, allow_visit=True)[0].status_code == 200
    at = arrival + timedelta(hours=1)
    set_time(e, at)
    assert door(e, rid, "open").status_code == 200
    assert door(e, rid, "enter").status_code == 200
    daily(e, at)
    assert choose(e, "check_in", sid)[0].status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    assert get(e, sid)["status"] == "completed"


def test_cancel_only_stops_bound_activities_and_preserves_a_new_independent_activity(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, _, _ = begin(e, jid, rid)
    with e.db.write() as c:
        player, npc = people(e, c)
        LifeActivityService.interrupt(c, e.npc, current_time(e), "本人先离开这次整夜休息。")
        independent = LifeActivityService.start(c, npc, current_time(e), "wait", 15)
    response, _ = choose(e, "cancel", sid)
    assert response.status_code in {200, 409}, response.text
    with e.db.write() as c:
        OvernightService.maintain(c, e.wid, current_time(e))
    assert get(e, sid)["status"] in {"cancelled", "interrupted"}
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM character_life_activities WHERE id=?", (independent,)
            ).fetchone()[0]
            == "running"
        )


def test_existing_appointment_prevents_overlapping_overnight_acceptance(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    at = current_time(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        prior = AppointmentService.apply(
            c,
            npc,
            player,
            at,
            AppointmentIntent(
                operation="propose",
                kind="talk",
                location_id=player["current_location_id"],
                meeting_world_time=to_iso(at + timedelta(hours=2)),
                minutes=15,
                statement="我们已有两小时后在此处见面十五分钟的明确安排。",
            ),
        )["appointment_id"]
    assert appointment_choice(e, "accept", prior).status_code == 200
    with pytest.raises(ValueError):
        sid = npc_offer(e, jid, rid)
        with e.db.write() as c:
            player, npc = people(e, c)
            OvernightService.apply(
                c,
                player,
                npc,
                current_time(e),
                OvernightIntent(
                    operation="accept",
                    stay_id=sid,
                    statement="我只接受能够与既有约会兼容的住宿。",
                ),
            )


@pytest.mark.parametrize("stage", ["propose", "accept"])
def test_existing_personal_activity_must_leave_time_to_walk_in_before_window_end(town, stage):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid = npc_offer(e, jid, rid) if stage == "accept" else None
    with e.db.write() as c:
        player, npc = people(e, c)
        LifeActivityService.start(c, player, current_time(e), "rest", 120)
    if stage == "propose":
        with pytest.raises(ValueError, match="到场窗"):
            npc_offer(e, jid, rid)
        assert journey(e, jid)["status"] == "active"
    else:
        response, _ = choose(e, "accept", sid)
        assert response.status_code == 409, response.text
        assert get(e, sid)["status"] == "pending"


def test_accepted_overnight_also_prevents_a_new_overlapping_appointment(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    accepted_stay(e, jid, rid)
    with e.db.read() as c:
        lid = people(e, c)[0]["current_location_id"]
    response = appointment_choice(
        e,
        "propose",
        kind="talk",
        location_id=lid,
        meeting_world_time=to_iso(current_time(e) + timedelta(hours=2)),
        minutes=15,
    )
    assert response.status_code == 409, response.text


def test_reinitialize_preserves_active_stay_and_same_activity_ids(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, _, _ = begin(e, jid, rid)
    bound = {a["id"] for a in activities(e, running=True)}
    e.db.initialize()
    assert get(e, sid)["status"] == "active"
    assert {a["id"] for a in activities(e, running=True)} == bound
    response = e.client.get(f"/api/worlds/{e.wid}/overnights?character_id={e.npc}")
    assert response.status_code == 200, response.text
    assert next(s for s in response.json() if s["id"] == sid)["journey_id"] == jid
    e.clock.heartbeat(e.wid, elapsed_seconds=8 * 3600)
    assert get(e, sid)["status"] == "completed"


def test_repeated_check_in_reuses_both_real_activities(town):
    e = town
    jid = stopped_long_journey(e)
    rid = room_at_stop(e, jid)
    sid, _, body = begin(e, jid, rid)
    bound = {a["id"] for a in activities(e)}
    response = e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["stay_id"] == sid
    assert {a["id"] for a in activities(e)} == bound
    assert len(bound) == 2


def test_legacy_social_receipt_ignores_only_new_null_overnight_choice(town):
    from world_engine.api_intimacy import NaturalSocialChoice
    from world_engine.shared_life import SharedLifeIntent

    e = town
    text = "我明确邀请你在这里休息五分钟。"
    body = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=text,
        evidence=text,
        shared_life_choice=SharedLifeIntent(
            operation="invite", kind="rest", minutes=5, statement=text
        ),
    ).model_dump(mode="json")
    body.pop("overnight_choice")
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    saved = {"status": "pending", "event_id": "old-receipt", "summary": "此前的本人选择已经记录"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, body["request_id"], e.player, digest, json.dumps(saved)),
        )
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body).json() == saved


@pytest.mark.parametrize(
    "changes",
    [
        {"minutes": 239},
        {"minutes": 721},
        {"window_minutes": 14},
        {"window_minutes": 121},
        {"minutes": True},
        {"room_id": None},
        {"journey_id": None},
        {"operation": "accept", "stay_id": "old"},
    ],
)
def test_overnight_terms_are_explicit_and_responses_cannot_rewrite_them(changes):
    with pytest.raises(ValueError):
        OvernightIntent(
            **(
                {
                    "operation": "propose",
                    "journey_id": "journey",
                    "room_id": "room",
                    "arrival_world_time": "2030-01-01T22:00:00+00:00",
                    "minutes": 480,
                    "window_minutes": 60,
                    "statement": "我只提出这份八小时住宿，回应时不重写时间地点。",
                }
                | changes
            )
        )
