"""哥哥，共乘文字与真实 HTTP 接口只在独立存档中验收。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.api_dialogue import NpcTextResult
from world_engine.api_intimacy import NaturalSocialChoice, compatible_choice_digests
from world_engine.decisions import NpcReply
from world_engine.intent_parser import IntentParseResult
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText
from world_engine.registration.models import ElementRegistrationSubmit
from world_engine.registration.registry import WorldElementRegistry
from world_engine.repository import to_iso
from world_engine.rides import RideIntent, RideService

WORDS = "我本人选择这份完整共乘安排。"


@pytest.fixture
def ride_world(trio):
    e = trio
    with e.db.write() as c:
        event = ActionService._record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="action.registration_source", actor_id=None, target_id=None,
            location_id=e.loc["id"], summary="隔离验收场景中，编年者核实一辆三座人力车的实物与停放位置。", payload={})
        registry = WorldElementRegistry()
        pending = registry.submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
            "requested_by_character_id": None, "source_event_id": event, "idempotency_key": str(uuid4()),
            "payload": {"element_type": "vehicle", "name": "三座人力车", "movement_type": "land",
                "speed_kmh": 12, "owner_character_id": e.player, "capacity": 3, "location_id": e.loc["id"]},
        }))
        assert pending.status == "proposed"
        e.vehicle = registry.confirm(c, world_id=e.wid, registration_id=pending.id).result_entity_id
    return e


def proposal(e, **changes):
    return {"operation": "propose", "vehicle_id": e.vehicle, "member_ids": e.ids,
        "controller_id": e.player, "destination_location_id": e.route[0],
        "deadline_world_time": to_iso(e.at + timedelta(hours=1)), "statement": WORDS, **changes}


def post(e, choice, **changes):
    payload = {"request_id": str(uuid4()), "recipient_id": e.npc,
        "intent": WORDS, "evidence": WORDS, "ride_choice": choice, **changes}
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload)


def rows(e):
    response = e.client.get(f"/api/worlds/{e.wid}/rides")
    assert response.status_code == 200, response.text
    return response.json()["rides"]


def answer(e, cid, rid, operation="accept", *, plain=False, mixed=False):
    result = {"reply": "我会按自己的真实意愿决定。"}
    if not plain:
        result["ride_intent"] = RideIntent(operation=operation, ride_id=rid, statement="我本人明确选择这份原共乘安排。")
    if mixed:
        result["group_journey_intent"] = {"operation": "end", "journey_id": "不该执行的另一安排", "statement": "同时结束另一行程。"}
    e.model.respond_to_player = lambda **kwargs: NpcReply(**result)
    response = e.client.post(f"/api/worlds/{e.wid}/player/act",
        json={"intent": "说：请你本人独立回应这份共乘。", "target_character_id": cid})
    assert response.status_code == 201, response.text
    return response


def accepted(e):
    response = post(e, proposal(e))
    assert response.status_code == 200, response.text
    rid = response.json()["ride_id"]
    for cid in (e.npc, e.third):
        answer(e, cid, rid)
    return rid


def test_api_preserves_three_independent_choices_and_separate_boarding(ride_world):
    e = ride_world
    response = post(e, proposal(e))
    assert response.status_code == 200, response.text
    rid = response.json()["ride_id"]
    assert [m["decision"] for m in rows(e)[0]["members"]] == ["accepted", "pending", "pending"]
    answer(e, e.npc, rid)
    assert rows(e)[0]["status"] == "pending"
    answer(e, e.third, rid)
    current = rows(e)[0]
    assert current["status"] == "accepted"
    assert all(not member["boarded_at"] for member in current["members"])
    assert post(e, {"operation": "depart", "ride_id": rid, "statement": WORDS}).status_code == 409
    assert post(e, {"operation": "board", "ride_id": rid, "statement": WORDS}).status_code == 200
    answer(e, e.npc, rid, "board")
    members = rows(e)[0]["members"]
    assert [bool(member["boarded_at"]) for member in members] == [True, True, False]
    assert post(e, {"operation": "depart", "ride_id": rid, "statement": WORDS}).status_code == 409
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE world_id=?", (e.wid,)).fetchone()[0] == 0
        assert len({member["decision_event_id"] for member in members}) == 3


@pytest.mark.parametrize("mixed", [False, True])
def test_plain_words_or_mixed_effects_never_become_npc_consent(ride_world, mixed):
    e = ride_world
    rid = post(e, proposal(e)).json()["ride_id"]
    answer(e, e.npc, rid, plain=not mixed, mixed=mixed)
    assert next(m for m in rows(e)[0]["members"] if m["character_id"] == e.npc)["decision"] == "pending"


def test_api_receipt_is_idempotent_and_cannot_change_vehicle_members_or_destination(ride_world):
    e = ride_world
    request_id = str(uuid4())
    choice = proposal(e)
    response = post(e, choice, request_id=request_id)
    assert response.status_code == 200, response.text
    assert post(e, choice, request_id=request_id).json() == response.json()
    for key, value in (("member_ids", [e.player, e.npc]), ("controller_id", e.npc),
            ("vehicle_id", str(uuid4())), ("destination_location_id", e.route[1])):
        assert post(e, {**choice, key: value}, request_id=request_id).status_code == 409
    assert post(e, choice, evidence="不是玩家本次原话").status_code == 422
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM vehicle_rides WHERE world_id=?", (e.wid,)).fetchone()[0] == 1
        source = json.loads(c.execute("SELECT payload_json FROM world_events WHERE id=?", (response.json()["event_id"],)).fetchone()[0])
        assert source["player_input"] == source["choice_evidence"] == WORDS


def test_api_rejects_unknown_vehicle_and_other_world_member_without_partial_proposal(ride_world):
    e = ride_world
    assert post(e, proposal(e, vehicle_id=str(uuid4()))).status_code == 409
    assert post(e, proposal(e, member_ids=[e.player, e.npc, str(uuid4())])).status_code == 409
    assert rows(e) == []
    assert e.client.get(f"/api/worlds/{uuid4()}/rides").status_code == 404


def test_api_cannot_board_on_behalf_of_another_character(ride_world):
    e = ride_world
    rid = accepted(e)
    choice = {"operation": "board", "ride_id": rid, "statement": WORDS}
    assert post(e, {**choice, "character_id": e.npc}).status_code == 422
    assert post(e, choice, player_id=e.npc).status_code == 422
    assert all(not member["boarded_at"] for member in rows(e)[0]["members"])


@pytest.mark.parametrize("boundary", ["proposed_at", "source_event", "learned_time", "unknown_source"])
def test_future_proposed_ride_never_enters_current_api_or_npc_context(ride_world, boundary):
    e = ride_world
    rid = post(e, proposal(e)).json()["ride_id"]
    with e.db.write() as c:
        # 哥哥，只设置隔离反例；当前时刻不能看到来自未来的提议事实。
        source = c.execute("SELECT proposal_event_id FROM vehicle_rides WHERE id=?", (rid,)).fetchone()[0]
        if boundary == "proposed_at":
            c.execute("UPDATE vehicle_rides SET proposed_at=? WHERE id=?", (to_iso(e.at + timedelta(hours=1)), rid))
        elif boundary == "source_event":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(hours=1)), source))
        elif boundary == "learned_time":
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=?", (to_iso(e.at + timedelta(hours=1)), source))
        else:
            c.execute("DELETE FROM character_event_knowledge WHERE event_id=?", (source,))
    assert rows(e) == []
    with e.db.read() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        assert RideService.context(c, npc, e.at) == []


def test_old_snapshot_does_not_receive_a_later_members_consent(ride_world):
    e = ride_world
    rid = post(e, proposal(e)).json()["ride_id"]
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        RideService.apply(c, npc, e.at + timedelta(seconds=1), RideIntent(operation="accept", ride_id=rid, statement=WORDS))
    with e.db.read() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        earlier = RideService.rows(c, player, e.at)[0]
        assert next(member for member in earlier["members"] if member["character_id"] == e.npc)["decision"] == "pending"


@pytest.mark.parametrize("boundary", ["decision_at", "decision_event", "boarded_at", "board_event"])
def test_member_future_decision_or_boarding_is_unknown_in_current_view(ride_world, boundary):
    e = ride_world
    rid = post(e, proposal(e)).json()["ride_id"]
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        RideService.apply(c, npc, e.at, RideIntent(operation="accept", ride_id=rid, statement=WORDS))
        if boundary.startswith("board"):
            third = c.execute("SELECT * FROM characters WHERE id=?", (e.third,)).fetchone()
            RideService.apply(c, third, e.at, RideIntent(operation="accept", ride_id=rid, statement=WORDS))
            RideService.apply(c, npc, e.at, RideIntent(operation="board", ride_id=rid, statement=WORDS))
        member = c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? AND character_id=?", (rid, e.npc)).fetchone()
        if boundary == "decision_at":
            c.execute("UPDATE vehicle_ride_members SET decision_at=? WHERE ride_id=? AND character_id=?", (to_iso(e.at + timedelta(seconds=1)), rid, e.npc))
        elif boundary == "boarded_at":
            c.execute("UPDATE vehicle_ride_members SET boarded_at=? WHERE ride_id=? AND character_id=?", (to_iso(e.at + timedelta(seconds=1)), rid, e.npc))
        else:
            event = member["decision_event_id" if boundary == "decision_event" else "board_event_id"]
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (to_iso(e.at + timedelta(seconds=1)), event))
    current = rows(e)[0]
    visible = next(member for member in current["members"] if member["character_id"] == e.npc)
    if boundary.startswith("decision"):
        assert visible["decision"] == "pending" and visible["decision_at"] is None and visible["decision_event_id"] is None
    else:
        assert visible["boarded_at"] is None and visible["board_event_id"] is None
    with e.db.read() as c:
        player = c.execute("SELECT * FROM characters WHERE id=?", (e.player,)).fetchone()
        packed = RideService.context(c, player, e.at)[0]
        contextual = next(member for member in packed["members"] if member["character_id"] == e.npc)
        if boundary.startswith("decision"):
            assert contextual["decision"] == "pending"
        else:
            assert not contextual.get("boarded_at") and not contextual.get("board_event_id")


def test_api_expired_reply_never_adds_npc_acceptance(ride_world, monkeypatch):
    e = ride_world
    rid = post(e, proposal(e)).json()["ride_id"]
    # 哥哥，时限通过真实心跳到达，暂不让无关的日常自主移动改变这次迟到回执场景。
    monkeypatch.setattr("world_engine.daily_life.DailyLifeService.tick", lambda *args, **kwargs: None)
    e.clock.heartbeat(e.wid, elapsed_seconds=3601)
    answer(e, e.npc, rid)
    assert next(m for m in rows(e)[0]["members"] if m["character_id"] == e.npc)["decision"] == "pending"
    assert rows(e)[0]["status"] == "expired"


@pytest.mark.parametrize("channel", ["letter", "outreach_letter"])
def test_remote_channel_cannot_accept_or_board_but_may_decline_own_original_ride(ride_world, channel):
    e = ride_world
    rid = post(e, proposal(e)).json()["ride_id"]
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        denied = settle_npc_reply(c, npc=npc, player_id=e.player, at=e.at, channel=channel,
            reply=NpcReply(reply="我本人回应原安排。", ride_intent=RideIntent(operation="accept", ride_id=rid, statement=WORDS)))
        assert denied.action_status == "rejected"
        allowed = settle_npc_reply(c, npc=npc, player_id=e.player, at=e.at, channel=channel,
            reply=NpcReply(reply="这次我不参加。", ride_intent=RideIntent(operation="decline", ride_id=rid, statement=WORDS)))
        assert allowed.action_status == "declined"
    assert rows(e)[0]["status"] == "declined"


def stub_preview(e, choice, *, evidence=WORDS, more=None):
    captured = []
    def complete(*, schema, **request):
        captured.append(request["user_payload"])
        return SimpleNamespace(data=schema.validate_python({"operation": "ride", "target_character_id": e.npc,
            "ride_choice": choice, "choice_evidence": evidence, **(more or {})}))
    e.model.complete = complete
    response = e.client.post(f"/api/worlds/{e.wid}/player/intents/preview",
        json={"intent": WORDS, "target_character_id": e.npc})
    assert response.status_code == 200, response.text
    return response.json(), captured


def test_semantic_preview_is_read_only_and_keeps_complete_real_proposal(ride_world):
    e = ride_world
    parsed, captured = stub_preview(e, proposal(e))
    assert parsed["operation"] == "ride" and parsed["route_kind"] == "social_choice"
    assert parsed["ride_choice"]["member_ids"] == e.ids
    assert rows(e) == [] and captured
    assert post(e, parsed["ride_choice"]).status_code == 200


@pytest.mark.parametrize("change", ["vehicle", "member", "controller", "destination", "evidence", "mixed"])
def test_semantic_preview_rejects_model_invented_choices(ride_world, change):
    e = ride_world
    choice = proposal(e)
    more = None
    evidence = WORDS
    if change == "vehicle":
        choice["vehicle_id"] = str(uuid4())
    elif change == "member":
        choice["member_ids"] = [e.player, e.npc, str(uuid4())]
    elif change == "controller":
        choice["controller_id"] = e.npc
    elif change == "destination":
        choice["destination_location_id"] = str(uuid4())
    elif change == "evidence":
        evidence = "模型新造的授权"
    else:
        more = {"group_journey_choice": {"operation": "end", "journey_id": str(uuid4()), "statement": WORDS}}
    parsed, _ = stub_preview(e, choice, evidence=evidence, more=more)
    assert parsed["operation"] == "none" and rows(e) == []


def test_preview_for_own_npc_proposal_never_accepts_for_another_member(ride_world):
    e = ride_world
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        rid = RideService.apply(c, npc, e.at, RideIntent(**proposal(e)))["ride_id"]
    parsed, _ = stub_preview(e, {"operation": "accept", "ride_id": rid, "statement": WORDS})
    assert parsed["route_kind"] == "social_choice"
    assert post(e, parsed["ride_choice"]).status_code == 200
    assert [m["decision"] for m in rows(e)[0]["members"]] == ["accepted", "accepted", "pending"]


def test_options_mask_unknown_names_and_hide_distant_or_unconfirmed_vehicles(ride_world):
    e = ride_world
    with e.db.write() as c:
        c.execute("DELETE FROM character_acquaintances WHERE observer_id=? AND subject_id=?", (e.player, e.third))
        c.execute("UPDATE characters SET name='不该泄露的真实姓名' WHERE id=?", (e.third,))
    options = e.client.get(f"/api/worlds/{e.wid}/rides/options")
    assert options.status_code == 200 and "不该泄露的真实姓名" not in options.text
    assert [v["id"] for v in options.json()["vehicles"]] == [e.vehicle]
    with e.db.write() as c:
        c.execute("UPDATE vehicle_states SET longitude=longitude+1 WHERE vehicle_id=?", (e.vehicle,))
    assert e.client.get(f"/api/worlds/{e.wid}/rides/options").json()["vehicles"] == []
    assert post(e, proposal(e)).status_code == 409


def test_all_text_schemas_preserve_typed_ride_intent():
    choice = {"operation": "decline", "ride_id": "original-ride", "statement": WORDS}
    for schema, raw in ((NpcReply, {"reply": "我这次不参加。"}),
            (NpcTextResult, {"reply": "我这次不参加。"}),
            (OutreachText, {"should_contact": True, "reply": "我这次不参加。"})):
        assert schema.model_validate({**raw, "ride_intent": choice}).ride_intent.operation == "decline"
    assert IntentParseResult.model_validate({"operation": "ride", "ride_choice": choice}).ride_choice.ride_id == "original-ride"


def test_new_null_ride_field_preserves_legacy_social_receipt_digest():
    payload = NaturalSocialChoice.model_validate({"request_id": "old-request-123", "recipient_id": "npc", "intent": WORDS,
        "evidence": WORDS, "companion_choice": {"operation": "propose", "minutes": 10, "statement": WORDS}})
    legacy = payload.model_dump(mode="json")
    legacy.pop("ride_choice")
    digest = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert digest in compatible_choice_digests(payload)


def moving_player_passenger(e):
    with e.db.write() as c:
        # 哥哥，这位路旁人物属于独立场景初始条件，没有加入本次共乘。
        c.execute("UPDATE characters SET longitude=longitude+0.0001 WHERE id=?", (e.third,))
        source = ActionService._record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="action.registration_source", actor_id=None, target_id=None, location_id=e.loc["id"],
            summary="隔离验收中核实 NPC 本人拥有的双座载具。", payload={})
        registry = WorldElementRegistry()
        pending = registry.submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
            "requested_by_character_id": None, "source_event_id": source, "idempotency_key": str(uuid4()),
            "payload": {"element_type": "vehicle", "name": "NPC 所有的双座车", "movement_type": "land",
                "speed_kmh": 12, "owner_character_id": e.npc, "capacity": 2, "location_id": e.loc["id"]},
        }))
        vehicle = registry.confirm(c, world_id=e.wid, registration_id=pending.id).result_entity_id
        def person(cid):
            return c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        rid = RideService.apply(c, person(e.player), e.at, RideIntent(**proposal(e,
            vehicle_id=vehicle, member_ids=[e.player,e.npc], controller_id=e.npc)))["ride_id"]
        RideService.apply(c, person(e.npc), e.at, RideIntent(operation="accept", ride_id=rid, statement=WORDS))
        for cid in (e.player,e.npc):
            RideService.apply(c, person(cid), e.at, RideIntent(operation="board", ride_id=rid, statement=WORDS))
        RideService.apply(c, person(e.npc), e.at, RideIntent(operation="depart", ride_id=rid, statement=WORDS))
    return rid


def physical_state(e, rid):
    with e.db.read() as c:
        return {
            "player": tuple(c.execute("SELECT longitude,latitude,current_location_id,current_room_id,space_layer,money,energy FROM characters WHERE id=?", (e.player,)).fetchone()),
            "members": [tuple(row) for row in c.execute("SELECT * FROM vehicle_ride_members WHERE ride_id=? ORDER BY ordinal", (rid,))],
            "records": c.execute("SELECT COUNT(*) FROM player_activity_records WHERE player_id=?", (e.player,)).fetchone()[0],
        }


@pytest.mark.parametrize("intent", ["动作：走到他身边", "动作：记录现场路况", "动作：开始工作"])
def test_moving_passenger_cannot_approach_outsider_or_start_stationary_work(ride_world, intent):
    e = ride_world
    rid = moving_player_passenger(e)
    before = physical_state(e, rid)
    response = e.client.post(f"/api/worlds/{e.wid}/player/act",
        json={"intent": intent, "target_character_id": e.third if "走到" in intent else e.npc})
    if "走到" in intent:
        # 哥哥，显式动作入口把同一个规则 ValueError 映射为既有 HTTP 400。
        assert response.status_code == 400 and "仍在真实载具上" in response.text, response.text
    else:
        assert response.status_code == 201, response.text
        assert response.json()["outcome"]["accepted"] is False
    assert physical_state(e, rid) == before
    with e.db.read() as c:
        assert RideService.aboard(c,e.player)
        assert c.execute("SELECT status FROM vehicle_rides WHERE id=?", (rid,)).fetchone()[0] == "moving"


def test_already_beside_same_vehicle_member_does_not_create_a_walk(ride_world):
    e = ride_world
    rid = moving_player_passenger(e)
    before = physical_state(e, rid)
    response = e.client.post(f"/api/worlds/{e.wid}/player/act",
        json={"intent": "动作：走到他身边", "target_character_id": e.npc})
    assert response.status_code == 201 and response.json()["outcome"]["accepted"], response.text
    assert physical_state(e, rid) == before


def test_repeated_board_with_new_request_preserves_original_player_and_npc_event_payloads(ride_world):
    e = ride_world
    rid = accepted(e)
    choice = {"operation": "board", "ride_id": rid, "statement": WORDS}
    original = post(e,choice)
    assert original.status_code == 200, original.text
    answer(e,e.npc,rid,"board")
    with e.db.read() as c:
        original_payloads = {row["id"]:row["payload_json"] for row in c.execute(
            "SELECT id,payload_json FROM world_events WHERE json_extract(payload_json,'$.ride_id')=?", (rid,))}
        npc_event = c.execute("SELECT board_event_id FROM vehicle_ride_members WHERE ride_id=? AND character_id=?", (rid,e.npc)).fetchone()[0]
        assert npc_event != original.json()["event_id"]
    repeat_words = "我只再次核对本人原登车回执。"
    repeated = post(e,choice,intent=repeat_words,evidence=repeat_words)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["event_id"] == original.json()["event_id"]
    with e.db.read() as c:
        after_payloads = {row["id"]:row["payload_json"] for row in c.execute(
            "SELECT id,payload_json FROM world_events WHERE json_extract(payload_json,'$.ride_id')=?", (rid,))}
    assert after_payloads == original_payloads
