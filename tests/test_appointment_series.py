# ruff: noqa: F811
"""哥哥，周期承诺独立同意、有限排程，每次相处仍须真正发生。"""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointment_rooms import room
from test_appointments import choose, get, people, set_time
from test_lived_world import town  # noqa: F401

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.intimacy import IntimacyService, RelationshipIntent
from world_engine.repository import from_iso, to_iso


def offer(e, **changes):
    with e.db.write() as c:
        player, npc = people(e, c)
        choice = AppointmentIntent(
            operation="propose",
            kind="talk",
            location_id=e.loc["id"],
            meeting_world_time=to_iso(e.at + timedelta(hours=1)),
            statement="我们每七个世界日见一次，共三次，每次聊十五分钟，你愿意吗？",
            **{"repeat_every_days": 7, "repeat_count": 3, **changes},
        )
        result = AppointmentService.apply(c, npc, player, e.at, choice)
        return result["appointment_id"], result["series_id"]


def series(e, sid):
    return next(
        row
        for row in e.client.get(f"/api/worlds/{e.wid}/appointments/series").json()
        if row["id"] == sid
    )


def finish_current(e, aid):
    row = get(e, aid)
    start = from_iso(row["starts_at"])
    set_time(e, start)
    response = choose(e, "attend", aid)
    assert response.status_code == 200, response.text
    e.clock.heartbeat(e.wid, elapsed_seconds=row["minutes"] * 60)
    assert get(e, aid)["status"] == "completed"


def current(e, sid):
    return next(
        o["appointment_id"]
        for o in series(e, sid)["occurrences"]
        if o["appointment_status"] in {"accepted", "active"}
    )


def test_accepting_only_today_does_not_accept_the_cycle(town):
    e = town
    aid, sid = offer(e)
    assert series(e, sid)["status"] == "pending"
    assert choose(e, "accept", aid).status_code == 200
    assert series(e, sid)["status"] == "ended"
    finish_current(e, aid)
    assert len(series(e, sid)["occurrences"]) == 1


def test_cycle_requires_mutual_confirmation_then_independent_actual_completions(town):
    e = town
    aid, sid = offer(e)
    assert choose(e, "accept", aid, accept_recurrence=True).status_code == 200
    assert series(e, sid)["status"] == "active"
    for index in range(3):
        active = current(e, sid)
        assert series(e, sid)["completed_count"] == index
        finish_current(e, active)
    result = series(e, sid)
    assert result["status"] == "completed" and result["completed_count"] == 3
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM social_appointments WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 3
        )
        assert (
            c.execute(
                "SELECT SUM(escrow) FROM contract_fulfillments WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 0
        )


def test_pause_cancels_future_only_and_resume_needs_the_other_person(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    paused = choose(e, "pause_series", series_id=sid)
    assert paused.status_code == 200, paused.text
    assert get(e, aid)["status"] == "cancelled"
    assert choose(e, "resume_series", series_id=sid).json()["status"] == "resume_pending"
    assert choose(e, "resume_series", series_id=sid).status_code == 409
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="resume_series", series_id=sid, statement="我也同意恢复剩下的周期安排。"
            ),
        )
    assert series(e, sid)["status"] == "active"
    assert get(e, current(e, sid))["starts_at"] == to_iso(e.at + timedelta(hours=1))


def test_ending_cycle_keeps_current_active_time_and_history(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    set_time(e, e.at + timedelta(hours=1))
    assert choose(e, "attend", aid).status_code == 200
    assert choose(e, "end_series", series_id=sid).status_code == 200
    assert get(e, aid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=15 * 60)
    assert get(e, aid)["status"] == "completed"
    assert len(series(e, sid)["occurrences"]) == 1 and series(e, sid)["status"] == "ended"


def test_cancel_one_occurrence_preserves_the_rest_of_confirmed_cycle(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    assert choose(e, "cancel", aid).status_code == 200
    with e.db.write() as c:
        AppointmentService.maintain(c, e.wid, e.at)
    assert series(e, sid)["status"] == "active" and current(e, sid) != aid


def test_late_unmaterialized_slots_are_skipped_without_fabricated_misses(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    now = e.at + timedelta(days=20)
    with e.db.write() as c:
        AppointmentService.maintain(c, e.wid, now)
    result = series(e, sid)
    assert (
        result["status"] == "completed"
        and result["missed_count"] == 1
        and result["completed_count"] == 0
    )
    assert [row["scheduling_status"] for row in result["occurrences"]] == [
        "scheduled",
        "skipped",
        "skipped",
    ]
    with e.db.write() as c:
        AppointmentService.maintain(c, e.wid, now + timedelta(days=1))
    assert len(series(e, sid)["occurrences"]) == 3


def test_new_conflicting_schedule_pauses_next_occurrence_with_reason(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    response = choose(
        e,
        "propose",
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(days=7, hours=1)),
    )
    assert response.status_code == 200, response.text
    second_id = response.json()["appointment_id"]
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="accept", appointment_id=second_id, statement="我同意这个单次安排。"
            ),
        )
    finish_current(e, aid)
    assert series(e, sid)["status"] == "paused" and "冲突" in series(e, sid)["reason"]
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM social_appointments WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 2
        )


def test_private_temporary_visit_cannot_be_extended_into_a_cycle(town):
    e = town
    rid = room(e)
    with pytest.raises(ValueError, match="临时室内接待"):
        with e.db.write() as c:
            player, npc = people(e, c)
            AppointmentService.apply(
                c,
                npc,
                player,
                e.at,
                AppointmentIntent(
                    operation="propose",
                    kind="talk",
                    location_id=e.loc["id"],
                    room_id=rid,
                    allow_visit=True,
                    meeting_world_time=to_iso(e.at + timedelta(hours=1)),
                    repeat_every_days=7,
                    repeat_count=3,
                    statement="我接待你本次来访，也想约以后见面。",
                ),
            )
    assert e.client.get(f"/api/worlds/{e.wid}/appointments").json() == []


def test_contact_boundary_pauses_existing_cycle_and_blocks_resume(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    with e.db.write() as c:
        player, npc = people(e, c)
        IntimacyService.apply(
            c,
            player,
            npc,
            e.at,
            RelationshipIntent(
                operation="boundary", pause_contact=True, statement="我想先独处，不要主动联系。"
            ),
        )
        AppointmentService.maintain(c, e.wid, e.at)
    assert series(e, sid)["status"] == "paused" and get(e, aid)["status"] == "cancelled"
    assert choose(e, "resume_series", series_id=sid).status_code == 409


def test_rescheduling_one_date_preserves_its_cycle_membership(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    response = choose(
        e,
        "reschedule",
        aid,
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=3)),
    )
    revised = response.json()["appointment_id"]
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="accept", appointment_id=revised, statement="这次改到三小时后。"
            ),
        )
    assert series(e, sid)["occurrences"][0]["appointment_id"] == revised
    finish_current(e, revised)
    assert get(e, current(e, sid))["starts_at"] == to_iso(e.at + timedelta(days=7, hours=1))


def test_pause_also_cancels_unconfirmed_rescheduling_request(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    revised = choose(
        e,
        "reschedule",
        aid,
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=3)),
    ).json()["appointment_id"]
    assert choose(e, "pause_series", series_id=sid).status_code == 200
    assert get(e, revised)["status"] == "cancelled"


def test_receipt_from_before_recurrence_fields_remains_idempotent(town):
    from world_engine.api_intimacy import NaturalSocialChoice

    e = town
    text = "我想一小时后在这里聊十五分钟。"
    payload = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "appointment_choice": {
            "operation": "propose",
            "kind": "talk",
            "location_id": e.loc["id"],
            "meeting_world_time": to_iso(e.at + timedelta(hours=1)),
            "statement": text,
        },
    }
    url = f"/api/worlds/{e.wid}/player/social-choices"
    first = e.client.post(url, json=payload)
    assert first.status_code == 200, first.text
    legacy = NaturalSocialChoice.model_validate(payload).model_dump(mode="json")
    for key in ("series_id", "repeat_every_days", "repeat_count", "accept_recurrence"):
        legacy["appointment_choice"].pop(key)
    digest = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with e.db.write() as c:
        c.execute(
            "UPDATE social_choice_receipts SET payload_hash=? WHERE world_id=?", (digest, e.wid)
        )
    assert e.client.post(url, json=payload).json() == first.json()


def test_rechecking_next_private_room_does_not_reuse_old_access(town):
    from world_engine.families import FamilyIntent, FamilyService

    e = town
    rid = room(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        FamilyService.apply(
            c,
            npc,
            player,
            e.at,
            FamilyIntent(
                operation="authorize_residence", room_id=rid, statement="我允许你居住在这间房间。"
            ),
        )
    aid, sid = offer(e, room_id=rid)
    assert choose(e, "accept", aid, accept_recurrence=True).status_code == 200
    assert choose(e, "cancel", aid).status_code == 200
    with e.db.write() as c:
        player, npc = people(e, c)
        FamilyService.apply(
            c,
            npc,
            player,
            e.at,
            FamilyIntent(
                operation="revoke_residence", room_id=rid, statement="我撤回这份居住许可。"
            ),
        )
        AppointmentService.maintain(c, e.wid, e.at)
    assert series(e, sid)["status"] == "paused" and "进入许可" in series(e, sid)["reason"]


def test_resume_reports_actual_paused_state_if_next_slot_is_still_blocked(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    choose(e, "pause_series", series_id=sid)
    other = choose(
        e,
        "propose",
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=1)),
    ).json()["appointment_id"]
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="accept", appointment_id=other, statement="我同意这个单次安排。"
            ),
        )
    choose(e, "resume_series", series_id=sid)
    with e.db.write() as c:
        player, npc = people(e, c)
        result = AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="resume_series", series_id=sid, statement="我也想恢复固定见面。"
            ),
        )
        assert result["status"] == "paused" and "冲突" in result["summary"]


def test_resume_after_old_start_skips_cancelled_slot_without_missed_penalty(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    choose(e, "pause_series", series_id=sid)
    now = e.at + timedelta(days=1)
    set_time(e, now)
    choose(e, "resume_series", series_id=sid)
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            now,
            AppointmentIntent(
                operation="resume_series", series_id=sid, statement="我也愿意恢复后续安排。"
            ),
        )
    result = series(e, sid)
    assert result["missed_count"] == 0 and get(e, aid)["status"] == "cancelled"
    assert get(e, current(e, sid))["starts_at"] == to_iso(e.at + timedelta(days=7, hours=1))


def test_resume_retains_individually_rescheduled_activity_time_and_audit(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    revised = choose(
        e,
        "reschedule",
        aid,
        kind="rest",
        minutes=20,
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=3)),
    ).json()["appointment_id"]
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="accept",
                appointment_id=revised,
                statement="我同意这次改为三小时后一起休息二十分钟。",
            ),
        )
    choose(e, "pause_series", series_id=sid)
    choose(e, "resume_series", series_id=sid)
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(operation="resume_series", series_id=sid, statement="我也同意恢复。"),
        )
    restored = get(e, current(e, sid))
    assert (
        restored["kind"] == "rest"
        and restored["minutes"] == 20
        and restored["starts_at"] == to_iso(e.at + timedelta(hours=3))
    )
    assert restored["replaces_id"] == revised and get(e, revised)["status"] == "cancelled"


def test_one_rescheduled_occurrence_cannot_jump_over_the_next_fixed_slot(town):
    e = town
    aid, sid = offer(e)
    choose(e, "accept", aid, accept_recurrence=True)
    response = choose(
        e,
        "reschedule",
        aid,
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(days=10, hours=1)),
    )
    assert response.status_code == 409 and "跨过下一次" in response.text
    assert get(e, aid)["status"] == "accepted" and len(series(e, sid)["occurrences"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"repeat_every_days": 0},
        {"repeat_count": True},
        {"repeat_count": 25},
        {"repeat_every_days": 90, "repeat_count": 24},
    ],
)
def test_invalid_or_unbounded_cycle_is_rejected_atomically(town, changes):
    e = town
    response = choose(
        e,
        "propose",
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=1)),
        **{"repeat_every_days": 7, "repeat_count": 3, **changes},
    )
    assert response.status_code in {409, 422}, response.text
    assert e.client.get(f"/api/worlds/{e.wid}/appointments/series").json() == []
