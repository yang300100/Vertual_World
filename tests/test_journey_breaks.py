"""哥哥，到站相处仍需双方同意，旅程、食物和停留时间分别核验。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointments import people
from test_journeys import advance_to_arrival, choose, get, npc_choice, offer
from test_lived_world import town  # noqa: F401

from world_engine.economy import EconomyService
from world_engine.journey_breaks import JourneyBreakService
from world_engine.repository import from_iso, to_iso
from world_engine.shared_life import SharedLifeIntent, SharedLifeService


def now(e, c):
    return from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
    )


def stopped(e):
    jid = offer(e)
    assert choose(e, "accept", jid).status_code == 200
    assert choose(e, "depart", jid).status_code == 200
    assert advance_to_arrival(e, jid)["phase"] == "stopped"
    return jid


def invite(e, jid, kind="rest", **changes):
    text = "我明确选择暂停这份原旅行，另行邀请你在已到站地点共同休息或用餐，不自动恢复行程。"
    body = {
        "request_id": str(uuid4()),
        "recipient_id": e.npc,
        "intent": text,
        "evidence": text,
        "shared_life_choice": {
            "operation": "invite",
            "kind": kind,
            "minutes": 5,
            "journey_id": jid,
            "statement": text,
            **changes,
        },
    }
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body), body


def respond(e, mid, op="accept"):
    with e.db.write() as c:
        player, npc = people(e, c)
        return SharedLifeService.apply(
            c,
            npc,
            player,
            now(e, c),
            SharedLifeIntent(
                operation=op, moment_id=mid, statement="这是我对本次到站相处的独立选择。"
            ),
        )


def test_rest_separate_consent_elapsed_time_frozen_dwell_and_mutual_resume(town):
    e = town
    jid = stopped(e)
    response, body = invite(e, jid)
    assert response.status_code == 200, response.text
    mid = response.json()["moment_id"]
    assert (
        e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=body).json()
        == response.json()
    )
    paused = get(e, jid)
    assert paused["status"] == "paused" and 119 < paused["remaining_stop_seconds"] <= 120
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE status='running' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()
    assert respond(e, mid)["status"] == "active"
    assert choose(e, "depart", jid).status_code == 409
    assert choose(e, "resume", jid).status_code == 409
    with pytest.raises(ValueError, match="用餐休息"):
        npc_choice(e, jid, "resume")
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert get(e, jid)["status"] == "paused"
    assert choose(e, "resume", jid).status_code == 200
    assert npc_choice(e, jid, "resume")["status"] == "active"
    assert choose(e, "depart", jid).status_code == 409
    e.clock.heartbeat(e.wid, elapsed_seconds=paused["remaining_stop_seconds"])
    assert choose(e, "depart", jid).status_code == 200
    assert advance_to_arrival(e, jid)["status"] == "completed"


def test_meal_invite_does_not_spend_or_consume_acceptance_consumes_real_food(town):
    e = town
    with e.db.write() as c:
        for _ in range(2):
            player = people(e, c)[0]
            assert EconomyService.buy_supply(c, player, e.at, food_only=True, budget=3)
    jid = stopped(e)
    with e.db.read() as c:
        before = c.execute(
            "SELECT SUM(quantity) FROM item_instances WHERE owner_character_id=? AND item_type_id=?",
            (e.player, e.food),
        ).fetchone()[0]
        wallet = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
    response, _ = invite(e, jid, "meal", meal_supply="proposer")
    assert response.status_code == 200, response.text
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT SUM(quantity) FROM item_instances WHERE owner_character_id=? AND item_type_id=?",
                (e.player, e.food),
            ).fetchone()[0]
            == before
        )
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()
    mid = response.json()["moment_id"]
    assert respond(e, mid)["status"] == "active"
    with e.db.read() as c:
        servings = c.execute(
            "SELECT * FROM shared_meal_servings WHERE moment_id=?", (mid,)
        ).fetchall()
        assert len(servings) == 2 and all(s["provider_id"] == e.player for s in servings)
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
            == wallet
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert get(e, jid)["status"] == "paused"


def test_missing_food_keeps_pending_invite_and_no_partial_activity_or_purchase(town):
    e = town
    jid = stopped(e)
    response, _ = invite(e, jid, "meal", meal_supply="proposer")
    mid = response.json()["moment_id"]
    with e.db.read() as c:
        money = people(e, c)[0]["money"]
    with pytest.raises(ValueError):
        respond(e, mid)
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM shared_life_moments WHERE id=?", (mid,)).fetchone()[0]
            == "pending"
        )
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()
        assert people(e, c)[0]["money"] == money
        assert not c.execute(
            "SELECT 1 FROM character_life_activities WHERE status='running' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()


@pytest.mark.parametrize("op", ["decline", "end"])
def test_decline_or_withdraw_does_not_restart_or_move_trip(town, op):
    e = town
    jid = stopped(e)
    response, _ = invite(e, jid)
    mid = response.json()["moment_id"]
    if op == "decline":
        respond(e, mid, op)
    else:
        with e.db.write() as c:
            player, npc = people(e, c)
            SharedLifeService.apply(
                c,
                player,
                npc,
                now(e, c),
                SharedLifeIntent(
                    operation="end", moment_id=mid, statement="我先撤回本次到站相处邀请。"
                ),
            )
    assert get(e, jid)["status"] == "paused"
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM character_movements WHERE status='moving' AND character_id IN (?,?)",
            (e.player, e.npc),
        ).fetchone()


@pytest.mark.parametrize(
    "state", ["pending", "accepted", "moving", "completed", "ended", "expired"]
)
def test_wrong_phase_or_ended_trip_cannot_create_break(town, state):
    e = town
    jid = offer(e)
    if state != "pending":
        choose(e, "accept", jid)
    if state in {"moving", "completed", "ended", "expired"}:
        choose(e, "depart", jid)
    if state == "completed":
        advance_to_arrival(e, jid)
        e.clock.heartbeat(e.wid, elapsed_seconds=120)
        choose(e, "depart", jid)
        advance_to_arrival(e, jid)
    if state == "ended":
        choose(e, "end", jid)
    if state == "expired":
        with e.db.write() as c:
            c.execute(
                "UPDATE joint_journeys SET deadline_at=? WHERE id=?",
                (to_iso(now(e, c) - timedelta(seconds=1)), jid),
            )
    response, _ = invite(e, jid)
    assert response.status_code == 409
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM journey_breaks").fetchone()


def test_pending_break_prevents_npc_accepting_resume(town):
    e = town
    jid = stopped(e)
    invite(e, jid)
    assert choose(e, "resume", jid).status_code == 409
    with pytest.raises(ValueError, match="用餐休息"):
        npc_choice(e, jid, "resume")


@pytest.mark.parametrize("changed", ["away", "ended"])
def test_acceptance_rechecks_real_scene_and_journey(town, changed):
    e = town
    jid = stopped(e)
    response, _ = invite(e, jid)
    if changed == "ended":
        choose(e, "end", jid)
    else:
        with e.db.write() as c:
            c.execute("UPDATE characters SET longitude=longitude+0.01 WHERE id=?", (e.npc,))
    with pytest.raises(ValueError):
        respond(e, response.json()["moment_id"])
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()


def test_reinitialize_preserves_link_and_source_evidence(town):
    e = town
    jid = stopped(e)
    response, _ = invite(e, jid)
    mid = response.json()["moment_id"]
    e.db.initialize()
    with e.db.read() as c:
        link = JourneyBreakService.link(c, mid)
        assert link["journey_id"] == jid
        assert (
            c.execute(
                "SELECT event_type FROM world_events WHERE id=?", (link["arrival_event_id"],)
            ).fetchone()[0]
            == "social.appointment_journey_arrived"
        )
        assert (
            c.execute(
                "SELECT event_type FROM world_events WHERE id=?", (link["pause_event_id"],)
            ).fetchone()[0]
            == "social.appointment_journey_paused"
        )
        row = next(
            m for m in SharedLifeService.rows(c, e.wid, e.player, now(e, c)) if m["id"] == mid
        )
        assert row["journey_id"] == jid and row["journey_stop_index"] == 0


def test_legacy_receipt_ignores_only_null_journey_reference(town):
    from world_engine.api_intimacy import NaturalSocialChoice

    e = town
    text = "我明确邀请你共同休息十五分钟。"
    payload = NaturalSocialChoice(
        request_id=str(uuid4()),
        recipient_id=e.npc,
        intent=text,
        evidence=text,
        shared_life_choice=SharedLifeIntent(operation="invite", kind="rest", statement=text),
    ).model_dump(mode="json")
    payload["shared_life_choice"].pop("journey_id")
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    saved = {"status": "pending", "event_id": "historical", "summary": "旧邀请已经保存"}
    with e.db.write() as c:
        c.execute(
            "INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)",
            (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)),
        )
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload).json() == saved


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "talk"},
        {"preparation_plan_id": "another"},
        {"operation": "accept", "moment_id": "old"},
    ],
)
def test_break_reference_is_only_valid_for_new_meal_or_rest(fields):
    with pytest.raises(ValueError):
        SharedLifeIntent(
            **(
                {
                    "operation": "invite",
                    "kind": "rest",
                    "journey_id": "trip",
                    "statement": "我只表达本人这次选择。",
                }
                | fields
            )
        )


def test_prompt_keeps_live_consent_and_latest_fact_without_forcing_old_speeches():
    from world_engine.roleplay_pipeline import build_npc_prompt

    pending = {
        "id": "new",
        "kind": "rest",
        "status": "pending",
        "statement": "我邀请你休息，之后另行商量恢复。",
        "journey_id": "trip",
    }
    closed = {
        "id": "past",
        "kind": "meal",
        "status": "completed",
        "minutes": 5,
        "source_event_id": "real_completion",
        "statement": "旧活动的完整原话" * 1000,
        "reason": "实际完成",
    }
    context = {
        "npc": {"name": "林澈"},
        "player_text": "你愿意在到站后休息吗？",
        "intimacy": {"shared_moments": [pending, closed]},
    }
    result = build_npc_prompt("只能表达本人选择。", context, max_input_tokens=1200)
    sent = json.loads(result.messages[1]["content"])["intimacy"]
    assert result.input_tokens <= 1200
    assert sent["shared_moments"][0] == pending
    assert sent["shared_moments"][1]["source_event_id"] == "real_completion"
    assert "statement" not in sent["shared_moments"][1]
    assert closed["statement"] == "旧活动的完整原话" * 1000
    assert not result.trace["intimacy.past_shared_moments:0"]["included"]


@pytest.mark.parametrize("cause", ["end", "deadline", "disabled"])
def test_invalid_pending_break_is_cancelled_and_does_not_lock_new_invites(town, cause):
    e = town
    jid = stopped(e)
    response, _ = invite(e, jid)
    mid = response.json()["moment_id"]
    if cause == "end":
        choose(e, "end", jid)
    else:
        with e.db.write() as c:
            if cause == "deadline":
                c.execute(
                    "UPDATE joint_journeys SET deadline_at=? WHERE id=?", (to_iso(now(e, c)), jid)
                )
            else:
                c.execute(
                    "UPDATE locations SET is_active=0 WHERE id=(SELECT location_id FROM journey_stops WHERE journey_id=? AND stop_index=0)",
                    (jid,),
                )
            JourneyBreakService.expire_pending(c, e.wid, now(e, c))
    with e.db.write() as c:
        assert (
            c.execute("SELECT status FROM shared_life_moments WHERE id=?", (mid,)).fetchone()[0]
            == "cancelled"
        )
        if cause == "disabled":
            c.execute(
                "UPDATE locations SET is_active=1 WHERE id=(SELECT location_id FROM journey_stops WHERE journey_id=? AND stop_index=0)",
                (jid,),
            )
        player, npc = people(e, c)
        result = SharedLifeService.apply(
            c,
            player,
            npc,
            now(e, c),
            SharedLifeIntent(
                operation="invite", kind="rest", statement="这是独立于旧旅行的新相处邀请。"
            ),
        )
        assert result["status"] == "pending"


def test_ending_trip_during_active_rest_keeps_separate_activity_and_honest_memory(town):
    e = town
    jid = stopped(e)
    response, _ = invite(e, jid)
    mid = response.json()["moment_id"]
    respond(e, mid)
    assert choose(e, "end", jid).status_code == 200
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    with e.db.read() as c:
        moment = c.execute("SELECT * FROM shared_life_moments WHERE id=?", (mid,)).fetchone()
        event = c.execute(
            "SELECT summary FROM world_events WHERE id=?", (moment["source_event_id"],)
        ).fetchone()[0]
        assert moment["status"] == "completed"
        assert get(e, jid)["status"] == "ended"
        assert "原旅行仍暂停" not in event and "不自动恢复" in event
