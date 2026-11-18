"""哥哥，改期要双方再次点头；等待期间原时间和个人租约都继续保留。"""

# ruff: noqa: F811
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_appointments import people
from test_journey_room_appointments import current_time
from test_lived_world import town  # noqa: F401
from test_lodging_handoff import prepared, proposal
from test_separate_stays import accept, choose, enter_player, get, set_time, setup

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.database import Database
from world_engine.repository import from_iso, to_iso
from world_engine.separate_stay_changes import SeparateStayChangeService
from world_engine.separate_stays import SeparateStayIntent, SeparateStayService


def accepted(e):
    jid, own, other, fields = setup(e)
    fields["arrival_world_time"] = to_iso(current_time(e) + timedelta(hours=3))
    sid = accept(e, fields)
    return sid, jid, own, other


def offer(e, sid, *, npc=False, shift=60):
    start = from_iso(get(e, sid)["arrival_starts_at"]) + timedelta(minutes=shift)
    return choose(e, "reschedule", sid, npc=npc, arrival_world_time=to_iso(start))


def request(e, rid):
    with e.db.read() as c:
        return dict(
            c.execute("SELECT * FROM journey_separate_stay_changes WHERE id=?", (rid,)).fetchone()
        )


def audit(e):
    with e.db.read() as c:
        return {
            "people": [
                tuple(r)
                for r in c.execute("SELECT id,money,current_room_id FROM characters ORDER BY id")
            ],
            "contracts": [
                tuple(r)
                for r in c.execute("SELECT * FROM contract_fulfillments ORDER BY request_id")
            ],
            "activities": [
                tuple(r) for r in c.execute("SELECT * FROM character_life_activities ORDER BY id")
            ],
            "movements": [
                tuple(r) for r in c.execute("SELECT * FROM character_movements ORDER BY id")
            ],
        }


def test_proposal_preserves_original_agreement_and_all_personal_resources(town):
    e = town
    sid, _, _, _ = accepted(e)
    before, resources = get(e, sid), audit(e)
    result = offer(e, sid)
    assert result["status"] == "pending" and get(e, sid) == before
    assert audit(e) == resources
    row = request(e, result["change_id"])
    assert row["base_event_id"] == before["source_event_id"]
    assert from_iso(row["expires_at"]) == from_iso(before["arrival_starts_at"])
    with e.db.read() as c:
        visible = SeparateStayService.rows(c, e.wid, e.player, current_time(e), e.npc)[0]
        assert visible["can_reschedule"]
        assert visible["reschedule_requests"][0]["id"] == result["change_id"]
        assert (
            not {"base_terms_json", "activity_id", "checked_in_at"}
            & visible["reschedule_requests"][0].keys()
        )


@pytest.mark.parametrize("npc_proposer", [False, True])
def test_other_person_accepts_new_time_and_original_personal_rooms_stay_fixed(town, npc_proposer):
    e = town
    sid, _, _, _ = accepted(e)
    before, resources = get(e, sid), audit(e)
    result = offer(e, sid, npc=npc_proposer)
    choice = choose(e, "accept_change", sid, npc=not npc_proposer, change_id=result["change_id"])
    assert choice["status"] == "accepted"
    row = get(e, sid)
    for key in SeparateStayChangeService.TIME_KEYS:
        assert from_iso(row[key]) == from_iso(before[key]) + timedelta(hours=1)
    assert row["reunion_starts_at"] == row["latest_end_at"]
    for key in (
        "proposer_id",
        "recipient_id",
        "own_room_id",
        "other_room_id",
        "source_lodging_plan_id",
        "journey_id",
        "minutes",
        "pause_event_id",
        "arrival_event_id",
        "statement",
        "response",
    ):
        assert row[key] == before[key]
    assert row["participants"] == before["participants"]
    assert row["source_event_id"] == choice["event_id"]
    assert audit(e) == resources
    with e.db.read() as c:
        npc_at = c.execute(
            "SELECT next_world_time FROM npc_daily_states WHERE character_id=?", (e.npc,)
        ).fetchone()[0]
        assert from_iso(npc_at) == current_time(e)
        payload = json.loads(
            c.execute(
                "SELECT payload_json FROM world_events WHERE id=?", (choice["event_id"],)
            ).fetchone()[0]
        )
        assert (
            payload["change_id"] == result["change_id"]
            and payload["old_arrival_starts_at"] == before["arrival_starts_at"]
        )


@pytest.mark.parametrize("operation,npc", [("decline_change", True), ("withdraw_change", False)])
def test_declining_or_withdrawing_keeps_original_schedule(town, operation, npc):
    e = town
    sid, _, _, _ = accepted(e)
    before = get(e, sid)
    rid = offer(e, sid)["change_id"]
    choose(e, operation, sid, npc=npc, change_id=rid)
    assert get(e, sid) == before
    assert request(e, rid)["status"] == (
        "declined" if operation == "decline_change" else "withdrawn"
    )
    assert offer(e, sid, shift=90)["status"] == "pending"


@pytest.mark.parametrize(
    "operation,npc",
    [("accept_change", False), ("decline_change", False), ("withdraw_change", True)],
)
def test_only_correct_change_participant_can_respond(town, operation, npc):
    e = town
    sid, _, _, _ = accepted(e)
    before = get(e, sid)
    rid = offer(e, sid)["change_id"]
    with pytest.raises(ValueError, match="只有"):
        choose(e, operation, sid, npc=npc, change_id=rid)
    assert get(e, sid) == before and request(e, rid)["status"] == "pending"


@pytest.mark.parametrize("shift", [-60, 60])
def test_expiry_occurs_before_the_earlier_window_without_rewriting_original(town, shift):
    e = town
    sid, _, _, _ = accepted(e)
    before = get(e, sid)
    rid = offer(e, sid, shift=shift)["change_id"]
    due = from_iso(request(e, rid)["expires_at"])
    with e.db.read() as c:
        assert (
            SeparateStayChangeService.boundary(c, e.wid, current_time(e), due + timedelta(hours=1))
            == due
        )
    set_time(e, due)
    with e.db.write() as c:
        SeparateStayService.maintain(c, e.wid, due)
    assert request(e, rid)["status"] == "expired"
    assert get(e, sid) == before
    with pytest.raises(ValueError, match="有效"):
        choose(e, "accept_change", sid, npc=True, change_id=rid)


def test_pending_new_time_never_replaces_original_checkin(town):
    e = town
    sid, _, own, _ = accepted(e)
    old = get(e, sid)
    rid = offer(e, sid)["change_id"]
    set_time(e, from_iso(old["arrival_starts_at"]))
    enter_player(e, own)
    result = choose(e, "check_in", sid)
    assert result["status"] == "active"
    assert request(e, rid)["status"] in {"expired", "cancelled"}
    with pytest.raises(ValueError):
        choose(e, "accept_change", sid, npc=True, change_id=rid)
    assert get(e, sid)["participants"][e.player]["activity_id"]


def test_cancel_original_cancels_only_pending_change_and_does_not_create_new_stay(town):
    e = town
    sid, _, _, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    choose(e, "cancel", sid)
    assert request(e, rid)["status"] == "cancelled"
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM journey_separate_stays").fetchone()[0] == 1


@pytest.mark.parametrize("corruption", ["source_event_id", "arrival_starts_at"])
def test_acceptance_rechecks_optimistic_base(town, corruption):
    e = town
    sid, _, _, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    with e.db.write() as c:
        row = get(e, sid)
        value = (
            row["pause_event_id"]
            if corruption == "source_event_id"
            else to_iso(from_iso(row[corruption]) + timedelta(minutes=1))
        )
        c.execute(f"UPDATE journey_separate_stays SET {corruption}=? WHERE id=?", (value, sid))
    changed = get(e, sid)
    with pytest.raises(ValueError):
        choose(e, "accept_change", sid, npc=True, change_id=rid)
    assert get(e, sid) == changed


def test_source_permission_endpoint_is_rechecked_when_moving_later(town):
    e = town
    pid, jid, own, other = prepared(e)
    fields = proposal(e, pid, jid, own, other)
    sid = accept(e, fields)
    before = get(e, sid)
    with e.db.write() as c:
        c.execute(
            "UPDATE journey_lodging_plans SET required_until_at=? WHERE id=?",
            (before["latest_end_at"], pid),
        )
    with pytest.raises(ValueError, match="许可终点"):
        offer(e, sid)
    assert get(e, sid) == before


def test_current_key_is_required_again_at_acceptance(town):
    e = town
    sid, _, own, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    with e.db.write() as c:
        key = str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,?,'tool',1)",
            (key, "改期测试独立钥匙"),
        )
        c.execute("UPDATE life_rooms SET door_locked=1 WHERE id=?", (own,))
        c.execute("UPDATE life_door_policies SET key_item_type_id=? WHERE room_id=?", (key, own))
    before = get(e, sid)
    with pytest.raises(ValueError, match="钥匙"):
        choose(e, "accept_change", sid, npc=True, change_id=rid)
    assert get(e, sid) == before


def test_only_one_pending_change_per_original_and_noop_cannot_write(town):
    e = town
    sid, _, _, _ = accepted(e)
    with pytest.raises(ValueError, match="相同"):
        offer(e, sid, shift=0)
    rid = offer(e, sid)["change_id"]
    with pytest.raises(ValueError, match="待回应"):
        offer(e, sid, shift=90)
    assert request(e, rid)["status"] == "pending"


def test_savepoint_rolls_back_accepted_change_when_original_update_fails(town):
    e = town
    sid, _, _, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    before = get(e, sid)
    before_resources = audit(e)
    with e.db.write() as c:
        c.execute(
            "CREATE TRIGGER fail_change_update BEFORE UPDATE OF arrival_starts_at "
            "ON journey_separate_stays BEGIN SELECT RAISE(ABORT,'改期写入测试失败'); END"
        )
    with pytest.raises(Exception, match="改期写入测试失败"):
        choose(e, "accept_change", sid, npc=True, change_id=rid)
    assert request(e, rid)["status"] == "pending" and get(e, sid) == before
    assert audit(e) == before_resources


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "reschedule", "stay_id": "stay"},
        {
            "operation": "reschedule",
            "stay_id": "stay",
            "arrival_world_time": "2030-01-01T12:00:00Z",
            "minutes": 600,
        },
        {
            "operation": "reschedule",
            "stay_id": "stay",
            "arrival_world_time": "2030-01-01T12:00:00Z",
            "own_room_id": "other",
        },
        {"operation": "accept_change", "stay_id": "stay"},
        {"operation": "accept", "stay_id": "stay", "change_id": "change"},
    ],
)
def test_change_schema_does_not_mutate_rooms_durations_or_old_response_semantics(changes):
    with pytest.raises(ValidationError):
        SeparateStayIntent(**changes, statement="这是我本人明确的分房选择。")


def test_new_schema_initialization_preserves_original_and_change_history(town):
    e = town
    sid, _, _, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    before, history = get(e, sid), request(e, rid)
    Database(e.db.path).initialize()
    Database(e.db.path).initialize()
    assert get(e, sid) == before and request(e, rid) == history


def test_unknown_change_or_other_stay_reference_cannot_be_accepted(town):
    e = town
    sid, _, _, _ = accepted(e)
    with pytest.raises(ValueError):
        choose(e, "accept_change", sid, npc=True, change_id=str(uuid4()))


def test_context_contains_only_pending_change_while_public_history_is_retained(town):
    e = town
    sid, _, _, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    with e.db.read() as c:
        pending = SeparateStayService.context(c, e.wid, e.npc, e.player, current_time(e))[0][
            "reschedule_requests"
        ]
        assert [r["id"] for r in pending] == [rid]
        assert "old_arrival_starts_at" not in pending[0]
    choose(e, "decline_change", sid, npc=True, change_id=rid)
    with e.db.read() as c:
        assert (
            SeparateStayService.context(c, e.wid, e.npc, e.player, current_time(e))[0][
                "reschedule_requests"
            ]
            == []
        )
        assert (
            SeparateStayService.rows(c, e.wid, e.player, current_time(e), e.npc)[0][
                "reschedule_requests"
            ][0]["status"]
            == "declined"
        )


def test_proposer_can_withdraw_own_change_from_own_room_without_contact_channel(town):
    e = town
    sid, _, own, _ = accepted(e)
    rid = offer(e, sid)["change_id"]
    before = get(e, sid)
    enter_player(e, own)
    assert choose(e, "withdraw_change", sid, change_id=rid)["status"] == "withdrawn"
    assert get(e, sid) == before


def test_lease_change_before_acceptance_cannot_extend_or_repay_original_contract(town):
    e = town
    pid, jid, own, other = prepared(e)
    sid = accept(e, proposal(e, pid, jid, own, other))
    rid = offer(e, sid)["change_id"]
    before = get(e, sid)
    with e.db.write() as c:
        c.execute(
            "UPDATE contract_fulfillments SET due_world_time=? WHERE asset_id=?",
            (to_iso(from_iso(before["latest_end_at"]) + timedelta(minutes=30)), other),
        )
    resources = audit(e)
    with pytest.raises(ValueError, match="许可|租约"):
        choose(e, "accept_change", sid, npc=True, change_id=rid)
    assert get(e, sid) == before and audit(e) == resources


def test_new_conflict_before_acceptance_preserves_the_original_confirmed_time(town):
    e = town
    sid, _, _, _ = accepted(e)
    rid = offer(e, sid, shift=120)["change_id"]
    before = get(e, sid)
    with e.db.write() as c:
        # 哥哥，隔离情境通过原约会服务独立确认另一份日程；未接受的改期不能先占新时间段。
        player, npc = people(e, c)
        result = AppointmentService.apply(
            c,
            player,
            npc,
            current_time(e),
            AppointmentIntent(
                operation="propose",
                kind="talk",
                location_id=before["location_id"],
                meeting_world_time=to_iso(
                    from_iso(before["reunion_ends_at"]) + timedelta(minutes=15)
                ),
                minutes=15,
                window_minutes=60,
                statement="我另约原分房会合结束十五分钟后在室外聊天。",
            ),
        )
        AppointmentService.apply(
            c,
            npc,
            player,
            current_time(e),
            AppointmentIntent(
                operation="accept",
                appointment_id=result["appointment_id"],
                statement="我独立同意这份后续室外约会。",
            ),
        )
    resources = audit(e)
    with pytest.raises(ValueError, match="冲突"):
        choose(e, "accept_change", sid, npc=True, change_id=rid)
    assert get(e, sid) == before and audit(e) == resources


def test_real_clock_expires_unanswered_change_and_npc_starts_at_original_time(town):
    e = town
    sid, _, _, _ = accepted(e)
    original = get(e, sid)
    rid = offer(e, sid, shift=-60)["change_id"]
    old_start = from_iso(original["arrival_starts_at"])
    e.clock.heartbeat(
        e.wid, elapsed_seconds=(old_start + timedelta(minutes=1) - current_time(e)).total_seconds()
    )
    row = get(e, sid)
    assert request(e, rid)["status"] == "expired"
    assert row["arrival_starts_at"] == original["arrival_starts_at"]
    part = row["participants"][e.npc]
    assert part["status"] == "active"
    assert old_start <= from_iso(part["checked_in_at"]) <= old_start + timedelta(minutes=1)
    assert row["participants"][e.player]["checked_in_at"] is None


def test_real_clock_does_not_start_at_old_time_after_both_accept_change(town):
    e = town
    sid, _, _, _ = accepted(e)
    original = get(e, sid)
    rid = offer(e, sid)["change_id"]
    choose(e, "accept_change", sid, npc=True, change_id=rid)
    old_start = from_iso(original["arrival_starts_at"])
    e.clock.heartbeat(
        e.wid, elapsed_seconds=(old_start + timedelta(minutes=1) - current_time(e)).total_seconds()
    )
    assert get(e, sid)["participants"][e.npc]["checked_in_at"] is None
    new_start = from_iso(get(e, sid)["arrival_starts_at"])
    e.clock.heartbeat(
        e.wid, elapsed_seconds=(new_start + timedelta(minutes=1) - current_time(e)).total_seconds()
    )
    part = get(e, sid)["participants"][e.npc]
    assert part["status"] == "active"
    assert new_start <= from_iso(part["checked_in_at"]) <= new_start + timedelta(minutes=1)
