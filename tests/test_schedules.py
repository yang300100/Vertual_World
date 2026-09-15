# ruff: noqa: F811
"""明确见面时间、改期竞态、日程冲突及身体状态触发重新安排。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import post, town  # noqa: F401

from world_engine.daily_life import DailyLifeService
from world_engine.repository import to_iso
from world_engine.schedules import ScheduleService


def meeting(env, hours=2, minutes=30, payment=0):
    request = post(
        env,
        "long-term-requests",
        {
            "recipient_id": env.npc,
            "operation_type": "约定",
            "terms": {
                "title": "河边见面",
                "payment": payment,
                "location_id": env.loc["id"],
                "meeting_world_time": to_iso(env.at + timedelta(hours=hours)),
                "meeting_minutes": minutes,
            },
        },
    )
    assert request["status"] == "npc_accepted", request
    post(env, f"long-term-requests/{request['id']}/confirm", {"accept_counter_terms": True})
    return request["id"]


def reschedule(env, parent, hours, revision=1):
    return post(
        env,
        "long-term-requests",
        {
            "recipient_id": env.npc,
            "operation_type": "约定改期",
            "terms": {
                "title": "重新商议时间",
                "payment": 0,
                "appointment_id": parent,
                "expected_revision": revision,
                "meeting_world_time": to_iso(env.at + timedelta(hours=hours)),
                "meeting_minutes": 30,
            },
        },
    )


def status(env, cid):
    with env.db.read() as c:
        return dict(
            c.execute("SELECT * FROM contract_fulfillments WHERE request_id=?", (cid,)).fetchone()
        )


def test_meeting_requires_speech_inside_explicit_time_window(town):
    e = town
    cid = meeting(e)
    post(
        e, "player/act", {"intent": "说话：这是见面之前的闲聊。", "target_character_id": e.npc}, 201
    )
    e.clock.heartbeat(e.wid, elapsed_seconds=0)
    assert status(e, cid)["status"] == "active"
    e.clock.heartbeat(e.wid, elapsed_seconds=7200)
    post(e, "player/act", {"intent": "说话：我按约定时间来了。", "target_character_id": e.npc}, 201)
    e.clock.heartbeat(e.wid, elapsed_seconds=0)
    assert status(e, cid)["status"] == "completed"


def test_rescheduling_is_confirmed_once_without_new_payment(town):
    e = town
    cid = meeting(e, payment=5)
    first = reschedule(e, cid, 3)
    second = reschedule(e, cid, 4)
    original = status(e, cid)
    assert original["due_world_time"] == to_iso(e.at + timedelta(hours=2, minutes=30))
    with e.db.read() as c:
        money = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
    post(e, f"long-term-requests/{first['id']}/confirm", {"accept_counter_terms": True})
    assert status(e, cid)["due_world_time"] == to_iso(e.at + timedelta(hours=3, minutes=30))
    assert status(e, cid)["escrow"] == 5
    post(e, f"long-term-requests/{second['id']}/confirm", {"accept_counter_terms": True}, 409)
    post(e, f"long-term-requests/{first['id']}/confirm", {"accept_counter_terms": True}, 409)
    e.db.initialize()
    with e.db.read() as c:
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == money
        )
        assert (
            c.execute(
                "SELECT count(*) FROM appointment_revisions WHERE contract_id=?", (cid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT revision FROM appointment_windows WHERE contract_id=?", (cid,)
            ).fetchone()[0]
            == 2
        )


def test_rescheduling_overdue_does_not_erase_prior_consequences(town):
    e = town
    cid = meeting(e)
    e.clock.heartbeat(e.wid, elapsed_seconds=10800)
    assert status(e, cid)["status"] == "overdue"
    amendment = reschedule(e, cid, 5)
    post(e, f"long-term-requests/{amendment['id']}/confirm", {"accept_counter_terms": True})
    assert status(e, cid)["status"] == "active"
    with e.db.read() as c:
        events = c.execute(
            "SELECT payload_json FROM world_events WHERE event_type='contract.overdue' AND world_id=?",
            (e.wid,),
        ).fetchall()
        assert any(json.loads(event[0])["request_id"] == cid for event in events)


@pytest.mark.parametrize(
    "change",
    [
        {"meeting_world_time": "2030-01-01T10:00:00"},
        {"meeting_world_time": "2000-01-01T10:00:00Z"},
        {"meeting_minutes": True},
        {"meeting_minutes": 0},
        {"location_id": "unknown-location"},
        {"location_id": {"fake": "nested"}},
    ],
)
def test_invalid_windows_are_rejected_before_confirmation(town, change):
    e = town
    terms = {
        "location_id": e.loc["id"],
        "meeting_world_time": to_iso(e.at + timedelta(hours=2)),
        "meeting_minutes": 30,
        "payment": 0,
        **change,
    }
    request = post(
        e, "long-term-requests", {"recipient_id": e.npc, "operation_type": "约定", "terms": terms}
    )
    assert request["status"] == "npc_rejected"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM appointment_windows WHERE world_id=?", (e.wid,)
        ).fetchone()


def test_overlapping_appointments_do_not_reveal_other_participants(town):
    e = town
    meeting(e, minutes=120)
    proposal = post(
        e,
        "long-term-requests",
        {
            "recipient_id": e.npc,
            "operation_type": "约定",
            "terms": {
                "location_id": e.loc["id"],
                "meeting_world_time": to_iso(e.at + timedelta(hours=3)),
                "meeting_minutes": 30,
                "payment": 0,
            },
        },
    )
    assert proposal["status"] == "npc_rejected"


def test_npc_reserves_time_instead_of_starting_long_production(town):
    e = town
    meeting(e)
    proposed = post(
        e,
        "activity-recipes",
        {
            "idempotency_key": str(uuid4()),
            "recipe": {
                "name": "一小时食品整理",
                "kind": "craft",
                "location_id": e.loc["id"],
                "duration_minutes": 60,
                "energy_cost": 10,
                "ingredients": [{"item_type_id": e.food, "quantity": 1}],
                "output_item_type_id": e.food,
                "output_quantity": 1,
            },
        },
        201,
    )
    post(e, f"registrations/{proposed['registration']['id']}/confirm")
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'",
            (e.npc,),
        ).fetchone()
    e.clock.heartbeat(e.wid, elapsed_seconds=3000)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT intention FROM npc_daily_states WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "已到约定地点，等候对方"
        )


def test_critical_needs_interrupt_work_once_and_refund_reserved_wage(town):
    e = town
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        before = c.execute(
            "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
        ).fetchone()[0]
        DailyLifeService._start(c, actor, e.at, "work")
        c.execute(
            "INSERT OR REPLACE INTO npc_daily_states(character_id,world_id,last_slot,next_world_time) VALUES (?,?,?,?)",
            (e.npc, e.wid, e.at.strftime("%Y-%m-%dT%H"), to_iso(e.at + timedelta(hours=1))),
        )
        c.execute("UPDATE characters SET satiety=5 WHERE id=?", (e.npc,))
        DailyLifeService.tick(c, e.wid, e.at + timedelta(minutes=1))
        assert (
            c.execute(
                "SELECT status FROM character_life_activities WHERE character_id=?", (e.npc,)
            ).fetchone()[0]
            == "interrupted"
        )
        after = c.execute(
            "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
        ).fetchone()[0]
        assert after == before
        DailyLifeService.tick(c, e.wid, e.at + timedelta(minutes=2))
        assert (
            c.execute(
                "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
            ).fetchone()[0]
            == after
        )


def test_schedule_risk_is_deduplicated_without_changing_appointment(town):
    e = town
    cid = meeting(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET health=30 WHERE id=?", (e.npc,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        for minute in (0, 1, 2):
            at = e.at + timedelta(minutes=minute)
            ScheduleService.record_assessment(c, actor, at, ScheduleService.assess(c, actor, at))
        assert (
            c.execute(
                "SELECT count(*) FROM world_events WHERE world_id=? AND event_type='schedule.at_risk'",
                (e.wid,),
            ).fetchone()[0]
            == 1
        )
    assert status(e, cid)["due_world_time"] == to_iso(e.at + timedelta(hours=2, minutes=30))


def test_player_time_skip_stops_at_meeting_start(town):
    e = town
    meeting(e, hours=1)
    aid = post(e, "player/life/activities", {"kind": "wait", "duration_minutes": 120}, 201)[
        "activity"
    ]["id"]
    version = e.client.get(f"/api/worlds/{e.wid}").json()["world"]["version"]
    result = post(
        e,
        f"player/life/activities/{aid}/advance",
        {"expected_version": version, "request_id": str(uuid4())},
    )
    assert result["heartbeat"]["world_delta_seconds"] == 3600


@pytest.mark.parametrize("change", ["reschedule", "recover"])
def test_obsolete_risk_messages_are_cancelled(town, change):
    e = town
    cid = meeting(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET health=30 WHERE id=?", (e.npc,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        ScheduleService.record_assessment(c, actor, e.at, ScheduleService.assess(c, actor, e.at))
        source = c.execute(
            "SELECT source_event_id FROM npc_schedule_assessments WHERE character_id=?", (e.npc,)
        ).fetchone()[0]
        job = str(uuid4())
        c.execute(
            "INSERT INTO npc_outreach_jobs(id,world_id,npc_id,player_id,channel,reason,world_day,source_event_id) VALUES (?,?,?,?,'in_person','日程风险',?,?)",
            (job, e.wid, e.npc, e.player, e.at.date().isoformat(), source),
        )
    if change == "reschedule":
        request = reschedule(e, cid, 4)
        post(e, f"long-term-requests/{request['id']}/confirm", {"accept_counter_terms": True})
    else:
        with e.db.write() as c:
            c.execute("UPDATE characters SET health=100 WHERE id=?", (e.npc,))
            actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            ScheduleService.record_assessment(
                c, actor, e.at, ScheduleService.assess(c, actor, e.at)
            )
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?", (job,)).fetchone()[0]
            == "cancelled"
        )
