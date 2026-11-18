# ruff: noqa: F811
"""哥哥，约会必须有双方同意、真实路程与完整相处，补约不能抹掉过去。"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_lived_world import town  # noqa: F401

from world_engine.appointments import AppointmentIntent, AppointmentService
from world_engine.collective_clock import next_boundary
from world_engine.contracts import ContractService
from world_engine.daily_life import DailyLifeService
from world_engine.decisions import NpcReply
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import process_outreach
from world_engine.repository import to_iso


def test_old_contract_delete_preserves_appointment_and_audit(town):
    e = town
    aid = offer(e)
    assert choose(e, "accept", aid).status_code == 200
    contract = get(e, aid)["contract_id"]
    response = e.client.delete(f"/api/worlds/{e.wid}/long-term-requests/{contract}")
    assert response.status_code == 200, response.text
    assert get(e, aid)["status"] == "cancelled"
    with e.db.read() as c:
        assert c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE request_id=?", (contract,)
        ).fetchone()


def test_worker_segments_ordinary_world_at_appointment_departure(town):
    e = town
    aid = offer(e)
    choose(e, "accept", aid)
    from world_engine.worker import WorldWorker

    worker = WorldWorker(e.settings)
    try:
        results = list(worker._world_heartbeats(e.wid, 7200))
        assert len(results) > 1
        assert results[0].current_time <= e.at + timedelta(minutes=50)
        assert get(e, aid)["status"] == "missed"
    finally:
        worker.engine.close()


def test_meal_needs_both_real_foods_before_starting(town):
    e = town
    response = choose(
        e,
        "propose",
        kind="meal",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=1)),
    )
    assert response.status_code == 200, response.text
    aid = response.json()["appointment_id"]
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(operation="accept", appointment_id=aid, statement="我同意一起用餐。"),
        )
        before = c.execute(
            "SELECT SUM(quantity) FROM item_instances WHERE owner_character_id=?", (e.npc,)
        ).fetchone()[0]
    set_time(e, e.at + timedelta(hours=1))
    assert choose(e, "attend", aid).status_code == 409
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT SUM(quantity) FROM item_instances WHERE owner_character_id=?", (e.npc,)
            ).fetchone()[0]
            == before
        )
        assert not c.execute(
            "SELECT 1 FROM shared_life_moments WHERE world_id=?", (e.wid,)
        ).fetchone()


def people(e, c):
    return [
        c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        for cid in (e.player, e.npc)
    ]


def choose(e, operation, aid=None, **kwargs):
    text = kwargs.pop("statement", "我明确选择这次安排。")
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/social-choices",
        json={
            "request_id": str(uuid4()),
            "recipient_id": e.npc,
            "intent": text,
            "evidence": text,
            "appointment_choice": {
                "operation": operation,
                "appointment_id": aid,
                "statement": text,
                **kwargs,
            },
        },
    )
    return response


def offer(e, *, at=None, **changes):
    with e.db.write() as c:
        player, npc = people(e, c)
        return AppointmentService.apply(
            c,
            npc,
            player,
            at or e.at,
            AppointmentIntent(
                operation="propose",
                kind="talk",
                location_id=e.loc["id"],
                meeting_world_time=to_iso((at or e.at) + timedelta(hours=1)),
                statement="一小时后到这里，留十五分钟聊聊，你愿意吗？",
                **changes,
            ),
        )["appointment_id"]


def get(e, aid):
    with e.db.read() as c:
        return dict(c.execute("SELECT * FROM social_appointments WHERE id=?", (aid,)).fetchone())


def set_time(e, at):
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))
        c.execute("UPDATE characters SET energy=90,satiety=100 WHERE world_id=?", (e.wid,))


def finish(e, aid, at):
    response = choose(e, "attend", aid)
    assert response.status_code == 200, response.text
    with e.db.write() as c:
        end = at + timedelta(minutes=15)
        # 独立验收只推进真实活动计时，不改变人物的位置或物品。
        from world_engine.life import LifeActivityService

        LifeActivityService.advance(c, e.wid, end, str(uuid4()))
    return get(e, aid)


def test_npc_offer_waits_for_player_and_creates_real_schedule_once(town):
    e = town
    aid = offer(e)
    assert get(e, aid)["status"] == "pending"
    with e.db.write() as c:
        player, npc = people(e, c)
        with pytest.raises(ValueError, match="自己的邀请"):
            AppointmentService.apply(
                c,
                npc,
                player,
                e.at,
                AppointmentIntent(operation="accept", appointment_id=aid, statement="我同意。"),
            )
        assert not c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE world_id=?", (e.wid,)
        ).fetchone()
    response = choose(e, "accept", aid)
    assert response.status_code == 200, response.text
    row = get(e, aid)
    assert row["status"] == "accepted" and row["proposer_id"] == e.npc
    assert choose(e, "accept", aid).status_code == 409
    with e.db.read() as c:
        contract = c.execute(
            "SELECT * FROM contract_fulfillments WHERE request_id=?", (row["contract_id"],)
        ).fetchone()
        assert contract["requester_id"] == e.player and contract["recipient_id"] == e.npc
        assert contract["escrow"] == 0


def test_arrival_and_one_conversation_do_not_complete_a_date(town):
    e = town
    aid = offer(e)
    assert choose(e, "accept", aid).status_code == 200
    assert choose(e, "attend", aid).status_code == 409
    start = e.at + timedelta(hours=1)
    set_time(e, start)
    e.client.post(
        f"/api/worlds/{e.wid}/player/act",
        json={"intent": "说话：我来了。", "target_character_id": e.npc},
    )
    with e.db.write() as c:
        ContractService.advance(c, e.wid, to_iso(start))
        AppointmentService.maintain(c, e.wid, start)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM appointment_attendance WHERE appointment_id=?", (aid,)
            ).fetchone()[0]
            == 2
        )
    assert get(e, aid)["status"] == "accepted"
    assert finish(e, aid, start)["status"] == "completed"


def test_attend_requires_actual_same_scene_and_busy_failure_is_atomic(town):
    e = town
    aid = offer(e)
    choose(e, "accept", aid)
    set_time(e, e.at + timedelta(hours=1))
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.npc,))
    assert choose(e, "attend", aid).status_code == 409
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (e.loc["longitude"], e.npc))
        from world_engine.life import LifeActivityService

        LifeActivityService.start(c, people(e, c)[1], e.at + timedelta(hours=1), "rest", 30)
    assert choose(e, "attend", aid).status_code == 409
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM shared_life_moments WHERE world_id=?", (e.wid,)
            ).fetchone()[0]
            == 0
        )


def test_reschedule_waits_for_other_and_old_contract_cannot_bypass_confirmation(town):
    e = town
    aid = offer(e)
    choose(e, "accept", aid)
    old = get(e, aid)
    response = choose(
        e,
        "reschedule",
        aid,
        kind="talk",
        location_id=e.loc["id"],
        meeting_world_time=to_iso(e.at + timedelta(hours=3)),
    )
    assert response.status_code == 200, response.text
    revised = response.json()["appointment_id"]
    assert get(e, aid)["status"] == "accepted"
    assert choose(e, "accept", revised).status_code == 409
    with e.db.write() as c:
        player, npc = people(e, c)
        AppointmentService.apply(
            c,
            npc,
            player,
            e.at,
            AppointmentIntent(
                operation="accept", appointment_id=revised, statement="我可以改到那时。"
            ),
        )
    assert get(e, aid)["status"] == "superseded" and get(e, revised)["status"] == "accepted"
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT status FROM contract_fulfillments WHERE request_id=?", (old["contract_id"],)
            ).fetchone()[0]
            == "cancelled"
        )


def test_missed_and_repair_require_new_mutual_actual_activity(town):
    e = town
    aid = offer(e)
    choose(e, "accept", aid)
    start = e.at + timedelta(hours=1)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.player,))
        AppointmentService.maintain(c, e.wid, start)
        AppointmentService.maintain(c, e.wid, start + timedelta(hours=1))
        player, npc = people(e, c)
        before = c.execute(
            "SELECT trust FROM relationships WHERE source_character_id=? AND target_character_id=?",
            (e.npc, e.player),
        ).fetchone()[0]
        AppointmentService.maintain(c, e.wid, start + timedelta(hours=2))
        assert (
            c.execute(
                "SELECT trust FROM relationships WHERE source_character_id=? AND target_character_id=?",
                (e.npc, e.player),
            ).fetchone()[0]
            == before
        )
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (e.loc["longitude"], e.player))
    assert get(e, aid)["status"] == "missed"
    now = e.at + timedelta(hours=3)
    set_time(e, now)
    repair = offer(e, at=now, repairs_id=aid)
    assert not get(e, aid)["repaired_by"]
    assert choose(e, "accept", repair).status_code == 200
    assert not get(e, aid)["repaired_by"]
    next_start = now + timedelta(hours=1)
    set_time(e, next_start)
    assert finish(e, repair, next_start)["status"] == "completed"
    assert get(e, aid)["status"] == "missed" and get(e, aid)["repaired_by"] == repair
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_events WHERE world_id=? AND event_type='social.appointment_repaired'",
                (e.wid,),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT trust FROM relationships WHERE source_character_id=? AND target_character_id=?",
                (e.npc, e.player),
            ).fetchone()[0]
            == before
        )


def test_expired_unaccepted_and_declined_are_not_missed(town):
    e = town
    aid = offer(e)
    with e.db.write() as c:
        AppointmentService.maintain(c, e.wid, e.at + timedelta(hours=1))
    assert get(e, aid)["status"] == "expired"
    later = offer(e, at=e.at + timedelta(hours=2))
    assert choose(e, "decline", later).status_code == 200
    with e.db.write() as c:
        AppointmentService.maintain(c, e.wid, e.at + timedelta(days=1))
    assert get(e, later)["status"] == "declined"


def test_cancel_active_interrupts_shared_time_without_credit(town):
    e = town
    aid = offer(e)
    choose(e, "accept", aid)
    set_time(e, e.at + timedelta(hours=1))
    assert choose(e, "attend", aid).status_code == 200
    assert choose(e, "cancel", aid).status_code == 200
    assert get(e, aid)["status"] == "interrupted"
    assert choose(e, "attend", aid).status_code == 409


def test_social_receipt_is_idempotent_even_after_world_time_changes(town):
    e = town
    aid = offer(e)
    text = "我愿意按这个时间赴约。"
    payload = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "appointment_choice": {"operation": "accept", "appointment_id": aid, "statement": text},
    }
    url = f"/api/worlds/{e.wid}/player/social-choices"
    first = e.client.post(url, json=payload)
    assert first.status_code == 200, first.text
    set_time(e, e.at + timedelta(days=1))
    assert e.client.post(url, json=payload).json() == first.json()
    payload["intent"] = payload["evidence"] = "我不愿意了。"
    assert e.client.post(url, json=payload).status_code == 409


def test_npc_outreach_can_propose_without_player_consent(town):
    e = town

    class Model:
        def complete(self, *, schema, **kwargs):
            return SimpleNamespace(
                data=schema.validate_python(
                    {
                        "should_contact": True,
                        "reply": "一小时后在这里聊聊？",
                        "appointment_intent": {
                            "operation": "propose",
                            "kind": "talk",
                            "location_id": e.loc["id"],
                            "meeting_world_time": to_iso(e.at + timedelta(hours=1)),
                            "statement": "一小时后在这里聊聊？",
                        },
                    }
                )
            )

    with e.db.write() as c:
        c.execute(
            "INSERT INTO npc_outreach_jobs(id,world_id,npc_id,player_id,channel,reason,world_day) VALUES (?,?,?,?,'in_person','商量未来安排',?)",
            (str(uuid4()), e.wid, e.npc, e.player, e.at.date().isoformat()),
        )
    from world_engine.engine import WorldEngine

    engine = WorldEngine(e.db, e.settings, e.model)
    engine.agent_model_backend = Model()
    result = process_outreach(engine, e.wid)
    with e.db.read() as c:
        jobs = [
            dict(row)
            for row in c.execute(
                "SELECT status,error FROM npc_outreach_jobs WHERE world_id=?", (e.wid,)
            )
        ]
    assert result == 1, jobs
    response = e.client.get(f"/api/worlds/{e.wid}/appointments")
    assert response.status_code == 200, response.text
    assert response.json()[0]["status"] == "pending"


def test_npc_decline_or_invalid_proposal_does_not_create_partial_schedule(town):
    e = town
    with e.db.write() as c:
        player, npc = people(e, c)
        result = settle_npc_reply(
            c,
            npc=npc,
            player_id=e.player,
            at=e.at,
            channel="letter",
            reply=NpcReply(
                reply="以后再约。",
                appointment_intent={
                    "operation": "propose",
                    "kind": "talk",
                    "location_id": "fake",
                    "meeting_world_time": to_iso(e.at + timedelta(hours=1)),
                    "statement": "一起聊聊？",
                },
            ),
        )
        assert result.action_status == "rejected"
        assert not c.execute(
            "SELECT 1 FROM social_appointments WHERE world_id=?", (e.wid,)
        ).fetchone()


def test_schedule_departure_is_a_real_clock_boundary_and_npc_walks(town):
    e = town
    aid = offer(e)
    choose(e, "accept", aid)
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.002 WHERE id=?", (e.npc,))
        c.execute("UPDATE npc_daily_states SET next_world_time=NULL WHERE character_id=?", (e.npc,))
        from world_engine.schedules import ScheduleService

        plan = ScheduleService.assess(c, people(e, c)[1], e.at)
        depart = plan["depart_at"]
        assert next_boundary(c, e.wid, e.at) <= depart
        DailyLifeService.tick(c, e.wid, depart, adjudication_due=False)
        assert c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (e.player,),
        ).fetchone()


@pytest.mark.parametrize(
    "changes",
    [
        {"meeting_world_time": "2030-01-01T09:00:00"},
        {"meeting_world_time": "2000-01-01T09:00:00Z"},
        {"location_id": "fake"},
        {"window_minutes": 5, "minutes": 15},
        {"minutes": True},
    ],
)
def test_invalid_invitation_cannot_create_event_or_schedule(town, changes):
    e = town
    kwargs = {
        "kind": "talk",
        "location_id": e.loc["id"],
        "meeting_world_time": to_iso(e.at + timedelta(hours=1)),
        **changes,
    }
    response = choose(e, "propose", **kwargs)
    assert response.status_code in {409, 422}, response.text
    assert e.client.get(f"/api/worlds/{e.wid}/appointments").json() == []
