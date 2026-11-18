"""哥哥，恢复选择的文字与接口只在独立世界检验，不替 NPC 作新同意。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_ride_text_api import accepted, answer, moving_player_passenger, post, ride_world, rows  # noqa: F401
from test_rides import ride_world as core_ride_world  # noqa: F401
from test_rides import advance as core_advance, arrival_seconds as core_arrival, boarded as core_boarded
from test_rides import choose as core_choose, current as core_now
from test_ride_runtime import rental

from world_engine.api_dialogue import NpcTextResult
from world_engine.api_intimacy import NaturalSocialChoice, compatible_choice_digests
from world_engine.decisions import NpcReply
from world_engine.contracts import ContractError, ContractService
from world_engine.intent_parser import IntentParseResult
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.rides import RideIntent, RideService

WORDS = "我本人明确选择本轮恢复原路线的完整条款。"


def now(e, c):
    return from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])


def current(e, rid):
    return next(row for row in rows(e) if row["id"] == rid)


def choose(e, rid, operation, recovery_id=None, **changes):
    choice = {"operation": operation, "ride_id": rid, "statement": WORDS, **changes}
    if recovery_id is not None:
        choice["recovery_id"] = recovery_id
    return choice


def submit(e, choice, **changes):
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json={
        "request_id": str(uuid4()), "recipient_id": e.npc, "intent": WORDS, "evidence": WORDS,
        "ride_choice": choice, **changes,
    })


def physical(e, rid):
    with e.db.read() as c:
        return {
            "ride": dict(c.execute("SELECT * FROM vehicle_rides WHERE id=?", (rid,)).fetchone()),
            "vehicle": dict(c.execute("SELECT s.* FROM vehicle_states s JOIN vehicle_rides r ON r.vehicle_id=s.vehicle_id WHERE r.id=?", (rid,)).fetchone()),
            "movement": dict(c.execute("SELECT m.* FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?", (rid,)).fetchone()),
            "people": [tuple(row) for row in c.execute("SELECT id,longitude,latitude,money FROM characters WHERE world_id=? ORDER BY id", (e.wid,))],
            "members": [tuple(row) for row in c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? ORDER BY ordinal", (rid,))],
        }


def block(e, rid, *, half=True):
    with e.db.read() as c:
        move = c.execute("SELECT m.* FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?", (rid,)).fetchone()
        seconds = (from_iso(move["estimated_arrival_world"]) - now(e, c)).total_seconds() / 2 if half else 0
    if seconds:
        e.clock.heartbeat(e.wid, elapsed_seconds=seconds)
    with e.db.write() as c:
        row = c.execute("SELECT * FROM vehicle_rides WHERE id=?", (rid,)).fetchone()
        RideService.suspend_vehicle(c, row["vehicle_id"], now(e, c), "隔离验收：途中条件曾中断，保留原路线与在车者等待另作恢复选择。")
    assert current(e, rid)["status"] == "blocked"


@pytest.fixture
def recovery_world(ride_world, request):
    e = ride_world
    rid = accepted(e)
    assert post(e, {"operation": "board", "ride_id": rid, "statement": WORDS}).status_code == 200
    for cid in (e.npc, e.third):
        answer(e, cid, rid, "board")
    response = post(e, {"operation": "depart", "ride_id": rid, "statement": WORDS})
    assert response.status_code == 200, response.text
    block(e, rid, half=getattr(request, "param", True))
    e.ride = rid
    return e


def propose(e):
    with e.db.read() as c:
        deadline = to_iso(now(e, c) + timedelta(hours=1))
    response = submit(e, choose(e, e.ride, "propose_recovery", deadline_world_time=deadline))
    assert response.status_code == 200, response.text
    return current(e, e.ride)["recovery"]["id"]


def npc_choice(e, cid, rid, recovery_id, operation="accept_recovery", *, plain=False, mixed=False):
    result = {"reply": "这次恢复我会按自己的意愿独立决定。"}
    if not plain:
        result["ride_intent"] = RideIntent(**choose(e, rid, operation, recovery_id))
    if mixed:
        result["companion_intent"] = {"operation": "propose", "minutes": 10, "statement": "同时提出另一份同行。"}
    e.model.respond_to_player = lambda **kwargs: NpcReply(**result)
    response = e.client.post(f"/api/worlds/{e.wid}/player/act", json={
        "intent": "说：请按你本人的情况决定这轮恢复，不需要勉强。", "target_character_id": cid,
    })
    assert response.status_code == 201, response.text
    return response


def preview(e, choice, *, evidence=WORDS, more=None):
    captured = []
    def complete(*, schema, **request):
        captured.append(request)
        return SimpleNamespace(data=schema.validate_python({"operation": "ride", "target_character_id": e.npc,
            "ride_choice": choice, "choice_evidence": evidence, **(more or {})}))
    e.model.complete = complete
    response = e.client.post(f"/api/worlds/{e.wid}/player/intents/preview", json={"intent": WORDS, "target_character_id": e.npc})
    assert response.status_code == 200, response.text
    return response.json(), captured


@pytest.mark.parametrize("operation", ["propose_recovery", "accept_recovery", "decline_recovery", "withdraw_recovery", "resume"])
def test_typed_channels_keep_the_exact_new_recovery_operation(operation):
    choice = {"operation": operation, "ride_id": "old-ride", "statement": WORDS}
    choice.update(deadline_world_time="2030-01-01T10:00:00+00:00") if operation == "propose_recovery" else choice.update(recovery_id="current-epoch")
    for schema, raw in ((NpcReply, {"reply": "本人选择。"}), (NpcTextResult, {"reply": "本人选择。"}),
            (OutreachText, {"should_contact": True, "reply": "本人选择。"})):
        typed = schema.model_validate({**raw, "ride_intent": choice}).ride_intent
        assert typed.operation == operation
        assert typed.ride_id == "old-ride"
    assert IntentParseResult.model_validate({"operation": "ride", "ride_choice": choice}).ride_choice.operation == operation


def test_semantic_preview_then_api_proposal_is_read_only_before_player_commit(recovery_world):
    e = recovery_world
    before = physical(e, e.ride)
    with e.db.read() as c:
        deadline = to_iso(now(e, c) + timedelta(hours=1))
    choice = choose(e, e.ride, "propose_recovery", deadline_world_time=deadline)
    parsed, captured = preview(e, choice)
    assert parsed["operation"] == "ride" and parsed["route_kind"] == "social_choice"
    assert physical(e, e.ride) == before and current(e, e.ride)["recovery"] is None
    assert captured and "propose_recovery" in captured[0]["system_prompt"]
    response = submit(e, parsed["ride_choice"])
    assert response.status_code == 200, response.text
    row = current(e, e.ride)
    assert row["status"] == "blocked" and row["recovery"]["status"] == "pending"
    assert [m["decision"] for m in row["recovery"]["members"]] == ["accepted", "pending", "pending"]
    after = physical(e, e.ride)
    assert after["ride"]["deadline_at"] == before["ride"]["deadline_at"]
    assert after["ride"]["terms_json"] == before["ride"]["terms_json"]
    assert after["members"] == before["members"] and after["movement"] == before["movement"]


def test_npc_new_consent_is_personal_and_never_automatically_resumes(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    before = physical(e, e.ride)
    npc_choice(e, e.npc, e.ride, recovery_id)
    first = current(e, e.ride)
    assert [m["decision"] for m in first["recovery"]["members"]] == ["accepted", "accepted", "pending"]
    assert first["status"] == "blocked"
    npc_choice(e, e.third, e.ride, recovery_id)
    ready = current(e, e.ride)
    assert ready["recovery"]["status"] == "accepted" and ready["status"] == "blocked"
    assert physical(e, e.ride)["movement"] == before["movement"]
    response = submit(e, choose(e, e.ride, "resume", recovery_id))
    assert response.status_code == 200, response.text
    resumed = physical(e, e.ride)
    assert resumed["ride"]["status"] == "moving"
    assert resumed["ride"]["movement_id"] == before["ride"]["movement_id"]
    assert resumed["movement"]["route_json"] == before["movement"]["route_json"]
    assert resumed["movement"]["distance_travelled_km"] == before["movement"]["distance_travelled_km"]
    assert resumed["people"] == before["people"]


@pytest.mark.parametrize("mixed", [False, True])
def test_plain_or_mixed_reply_cannot_replace_new_npc_consent(recovery_world, mixed):
    e = recovery_world
    recovery_id = propose(e)
    before = physical(e, e.ride)
    npc_choice(e, e.npc, e.ride, recovery_id, plain=not mixed, mixed=mixed)
    row = current(e, e.ride)
    assert next(m for m in row["recovery"]["members"] if m["character_id"] == e.npc)["decision"] == "pending"
    assert physical(e, e.ride)["movement"] == before["movement"] and row["status"] == "blocked"


def test_decline_retains_the_vehicle_and_all_old_personal_choices(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    before = physical(e, e.ride)
    npc_choice(e, e.npc, e.ride, recovery_id, "decline_recovery")
    row = current(e, e.ride)
    assert row["recovery"]["status"] == "declined" and row["status"] == "blocked"
    assert physical(e, e.ride)["movement"] == before["movement"]
    assert physical(e, e.ride)["members"] == before["members"]
    assert submit(e, choose(e, e.ride, "resume", recovery_id)).status_code == 409


def test_accepting_member_can_withdraw_without_erasing_their_accept_event(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    npc_choice(e, e.npc, e.ride, recovery_id)
    original = next(m for m in current(e, e.ride)["recovery"]["members"] if m["character_id"] == e.npc)
    npc_choice(e, e.npc, e.ride, recovery_id, "withdraw_recovery")
    row = current(e, e.ride)
    after = next(m for m in row["recovery"]["members"] if m["character_id"] == e.npc)
    assert row["recovery"]["status"] == "withdrawn" and row["status"] == "blocked"
    assert after["decision"] == "accepted" and after["decision_event_id"] == original["decision_event_id"]
    assert submit(e, choose(e, e.ride, "resume", recovery_id)).status_code == 409


def test_old_epoch_response_is_not_a_response_to_the_new_proposal(recovery_world):
    e = recovery_world
    first = propose(e)
    assert submit(e, choose(e, e.ride, "withdraw_recovery", first)).status_code == 200
    second = propose(e)
    assert second != first and current(e, e.ride)["recovery"]["epoch"] == 2
    npc_choice(e, e.npc, e.ride, first)
    row = current(e, e.ride)
    assert row["recovery"]["id"] == second
    assert next(m for m in row["recovery"]["members"] if m["character_id"] == e.npc)["decision"] == "pending"


def test_new_stop_request_invalidates_prior_recovery_without_moving_anybody(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    before = physical(e, e.ride)
    assert submit(e, choose(e, e.ride, "request_stop")).status_code == 200
    assert submit(e, choose(e, e.ride, "resume", recovery_id)).status_code == 409
    assert current(e, e.ride)["status"] == "blocked"
    after = physical(e, e.ride)
    assert after["ride"]["stop_requested"] and after["movement"] == before["movement"]
    assert after["people"] == before["people"]


@pytest.mark.parametrize("recovery_world", [False], indirect=True)
def test_member_can_leave_at_real_safe_origin_and_old_recovery_cannot_reuse_them(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    npc_choice(e, e.npc, e.ride, recovery_id)
    answer(e, e.npc, e.ride, "disembark")
    row = current(e, e.ride)
    assert next(m for m in row["members"] if m["character_id"] == e.npc)["disembarked_at"]
    assert submit(e, choose(e, e.ride, "resume", recovery_id)).status_code == 409
    assert current(e, e.ride)["status"] == "blocked"


def test_duplicate_requests_keep_old_events_and_do_not_drive_a_second_time(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    request = str(uuid4())
    choice = choose(e, e.ride, "withdraw_recovery", recovery_id)
    first = submit(e, choice, request_id=request)
    assert first.status_code == 200, first.text
    with e.db.read() as c:
        events = [tuple(row) for row in c.execute("SELECT id,payload_json FROM world_events WHERE world_id=? ORDER BY rowid", (e.wid,))]
    assert submit(e, choice, request_id=request).json() == first.json()
    with e.db.read() as c:
        assert events == [tuple(row) for row in c.execute("SELECT id,payload_json FROM world_events WHERE world_id=? ORDER BY rowid", (e.wid,))]
    assert submit(e, {**choice, "recovery_id": str(uuid4())}, request_id=request).status_code == 409


def test_missing_new_null_recovery_id_keeps_a_legacy_ride_receipt_digest():
    payload = NaturalSocialChoice.model_validate({"request_id": "legacy-ride-request", "recipient_id": "npc", "intent": WORDS,
        "evidence": WORDS, "ride_choice": {"operation": "board", "ride_id": "old-ride", "statement": WORDS}})
    legacy = payload.model_dump(mode="json")
    legacy["ride_choice"].pop("recovery_id")
    digest = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert digest in compatible_choice_digests(payload)


@pytest.mark.parametrize("change", ["unknown_recovery", "old_epoch", "another_ride", "evidence", "mixed"])
def test_semantic_preview_cannot_invent_a_fresh_recovery_authorization(recovery_world, change):
    e = recovery_world
    recovery_id = propose(e)
    choice = choose(e, e.ride, "withdraw_recovery", recovery_id)
    evidence, extra = WORDS, None
    if change == "unknown_recovery":
        choice["recovery_id"] = str(uuid4())
    elif change == "old_epoch":
        assert submit(e, choose(e, e.ride, "withdraw_recovery", recovery_id)).status_code == 200
        propose(e)
    elif change == "another_ride":
        choice["ride_id"] = str(uuid4())
    elif change == "evidence":
        evidence = "不是本轮玩家原话。"
    else:
        extra = {"companion_choice": {"operation": "propose", "minutes": 10, "statement": WORDS}}
    parsed, _ = preview(e, choice, evidence=evidence, more=extra)
    assert parsed["operation"] == "none" and parsed.get("ride_choice") is None
    assert current(e, e.ride)["status"] == "blocked"


@pytest.mark.parametrize("boundary", ["proposal_time", "source_event", "knowledge_time", "unknown_source"])
def test_recovery_from_future_or_unknown_source_is_not_current_knowledge(recovery_world, boundary):
    e = recovery_world
    recovery_id = propose(e)
    with e.db.write() as c:
        future = to_iso(now(e, c) + timedelta(minutes=1))
        source = c.execute("SELECT proposal_event_id FROM vehicle_ride_recoveries WHERE id=?", (recovery_id,)).fetchone()[0]
        if boundary == "proposal_time":
            c.execute("UPDATE vehicle_ride_recoveries SET proposed_at=? WHERE id=?", (future, recovery_id))
        elif boundary == "source_event":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, source))
        elif boundary == "knowledge_time":
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=?", (future, source))
        else:
            c.execute("DELETE FROM character_event_knowledge WHERE event_id=?", (source,))
    assert current(e, e.ride)["recovery"] is None
    parsed, _ = preview(e, choose(e, e.ride, "withdraw_recovery", recovery_id))
    assert parsed["operation"] == "none"


@pytest.mark.parametrize("boundary", ["decision_time", "source_event", "knowledge_time"])
def test_future_npc_recovery_acceptance_is_not_shown_or_reused(recovery_world, boundary):
    e = recovery_world
    recovery_id = propose(e)
    npc_choice(e, e.npc, e.ride, recovery_id)
    with e.db.write() as c:
        future = to_iso(now(e, c) + timedelta(minutes=1))
        member = c.execute("SELECT * FROM vehicle_ride_recovery_members WHERE recovery_id=? AND character_id=?", (recovery_id, e.npc)).fetchone()
        if boundary == "decision_time":
            c.execute("UPDATE vehicle_ride_recovery_members SET decision_at=? WHERE recovery_id=? AND character_id=?", (future, recovery_id, e.npc))
        elif boundary == "source_event":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, member["decision_event_id"]))
        else:
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=? AND character_id=?", (future, member["decision_event_id"], e.player))
    row = current(e, e.ride)
    visible = next(m for m in row["recovery"]["members"] if m["character_id"] == e.npc)
    assert visible["decision"] == "pending" and visible["decision_event_id"] is None
    assert row["status"] == "blocked" and not row["recovery"]["can_resume"]


@pytest.mark.parametrize("channel", ["letter", "outreach_letter"])
def test_remote_npc_controller_cannot_resume_while_preserving_accepted_recovery(ride_world, channel):
    e = ride_world
    rid = moving_player_passenger(e)
    block(e, rid)
    e.ride = rid
    recovery_id = propose(e)
    npc_choice(e, e.npc, rid, recovery_id)
    before = physical(e, rid)
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        result = settle_npc_reply(c, npc=npc, player_id=e.player, at=now(e, c), channel=channel,
            reply=NpcReply(reply="我本人决定是否恢复驾驶。", ride_intent=RideIntent(**choose(e, rid, "resume", recovery_id))))
    assert result.action_status == "rejected"
    assert physical(e, rid)["movement"] == before["movement"] and current(e, rid)["status"] == "blocked"


@pytest.fixture
def blocked_rental_world(core_ride_world):
    e = core_ride_world
    original = rental(e, seconds=3600)
    rid = core_boarded(e)
    core_choose(e, e.player, "depart", rid)
    core_advance(e, core_arrival(e, rid) / 2)
    with e.db.write() as c:
        old = c.execute("SELECT * FROM contract_fulfillments WHERE request_id=?", (original,)).fetchone()
        ContractService.cancel(c, old, to_iso(core_now(e, c)))
        # 哥哥，充足资金属于隔离负向场景条件，避免把权限拒绝误判为缺钱拒绝。
        c.execute("UPDATE characters SET money=100 WHERE id IN (?,?)", (e.player, e.other))
    e.ride = rid
    return e


def insert_reacquisition(c, e, *, requester=None, status="npc_accepted", counter=None):
    terms = {"vehicle_id": e.vehicle, "payment": 5, "duration_days": 1}
    request = str(uuid4())
    c.execute("INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,terms_json,status,counter_terms_json,created_at,updated_at) VALUES (?,?,?,?,'lease',?,?,?,?,?)",
        (request, e.wid, requester or e.player, e.npc, json.dumps(terms), status,
            json.dumps(counter) if counter is not None else None, to_iso(utc_now()), to_iso(utc_now())))
    return dict(c.execute("SELECT * FROM long_term_operation_requests WHERE id=?", (request,)).fetchone()), terms


def reacquisition_state(c, e):
    return {
        "wallets": [tuple(row) for row in c.execute("SELECT id,money FROM characters WHERE world_id=? ORDER BY id", (e.wid,))],
        "ride": tuple(c.execute("SELECT * FROM vehicle_rides WHERE id=?", (e.ride,)).fetchone()),
        "movement": tuple(c.execute("SELECT m.* FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?", (e.ride,)).fetchone()),
        "vehicle": tuple(c.execute("SELECT * FROM vehicle_states WHERE vehicle_id=?", (e.vehicle,)).fetchone()),
        "members": [tuple(row) for row in c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? ORDER BY ordinal", (e.ride,))],
        "contracts": [tuple(row) for row in c.execute("SELECT * FROM contract_fulfillments WHERE asset_id=? ORDER BY rowid", (e.vehicle,))],
        "recoveries": c.execute("SELECT COUNT(*) FROM vehicle_ride_recoveries WHERE ride_id=?", (e.ride,)).fetchone()[0],
    }


@pytest.mark.parametrize("invalid", ["third_person", "not_accepted", "rejected", "forged_status", "payment_changed", "duration_changed", "counter_changed"])
def test_blocked_vehicle_reacquisition_never_accepts_third_person_or_unaccepted_changed_terms(blocked_rental_world, invalid):
    e = blocked_rental_world
    with e.db.write() as c:
        request, terms = insert_reacquisition(c, e,
            requester=e.other if invalid == "third_person" else e.player,
            status="submitted" if invalid in {"not_accepted", "forged_status"} else "npc_rejected" if invalid == "rejected" else "npc_countered" if invalid == "counter_changed" else "npc_accepted",
            counter={"vehicle_id": e.vehicle, "payment": 7, "duration_days": 1} if invalid == "counter_changed" else None)
        if invalid == "forged_status":
            request["status"] = "npc_accepted"
        elif invalid == "payment_changed":
            terms["payment"] = 6
        elif invalid == "duration_changed":
            terms["duration_days"] = 2
        before = reacquisition_state(c, e)
        with pytest.raises(ContractError):
            ContractService.start(c, request, terms, to_iso(core_now(e, c)))
        assert reacquisition_state(c, e) == before


def test_reading_reacquisition_permission_never_charges_or_resumes_and_real_payment_keeps_ride_blocked(blocked_rental_world):
    e = blocked_rental_world
    with e.db.write() as c:
        request, terms = insert_reacquisition(c, e)
        ride = c.execute("SELECT * FROM vehicle_rides WHERE id=?", (e.ride,)).fetchone()
        before = reacquisition_state(c, e)
        assert ContractService._blocked_lease_reacquisition(c, ride, request, ContractService.normalize("lease", terms), core_now(e, c))
        assert reacquisition_state(c, e) == before
        ContractService.start(c, request, terms, to_iso(core_now(e, c)))
        after = reacquisition_state(c, e)
        assert after["ride"] == before["ride"] and after["movement"] == before["movement"]
        assert after["vehicle"] == before["vehicle"] and after["members"] == before["members"]
        assert after["recoveries"] == 0 and c.execute("SELECT status FROM vehicle_rides WHERE id=?", (e.ride,)).fetchone()[0] == "blocked"
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == dict(before["wallets"])[e.player] - 5
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == dict(before["wallets"])[e.npc] + 5
        assert c.execute("SELECT COUNT(*) FROM contract_fulfillments WHERE asset_id=? AND status='active'", (e.vehicle,)).fetchone()[0] == 1
