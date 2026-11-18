"""哥哥，引用共同订房只是新邀请的来源，不补出另一人的同意。"""

# ruff: noqa: F811
import copy
import hashlib
import json
from uuid import uuid4

import pytest
from test_lived_world import town  # noqa: F401
from test_lodging_handoff import prepared, proposal
from test_journey_room_appointments import current_time
from test_separate_stays_text import preview
from test_separate_stays_api import body, post

from world_engine.api_intimacy import NaturalSocialChoice
from world_engine.lodging_plans import LodgingPlanService
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt
from world_engine.separate_stays import SeparateStayIntent


@pytest.mark.parametrize("original_proposer", ["player", "npc"])
@pytest.mark.parametrize("failure", [None, "unknown", "swapped", "journey"])
def test_parser_resolves_only_shared_source_and_preserves_current_person_room_mapping(original_proposer, failure):
    source = dict(id="paid-source", journey_id="trip", proposer_id=original_proposer,
                  own_room_id="my-room" if original_proposer == "player" else "your-room",
                  other_room_id="your-room" if original_proposer == "player" else "my-room")
    choice = SeparateStayIntent(operation="propose", source_lodging_plan_id="paid-source",
        journey_id="trip", own_room_id="my-room", other_room_id="your-room",
        arrival_world_time="2030-01-01T22:00:00Z", statement="按我们刚核对的订房，我另提今晚十点各住原来的房间休息八小时，醒后原站会合。")
    context = {"journeys":[dict(id="trip",status="paused",phase="stopped")],
               "separate_stay_rooms":[dict(id="my-room"),dict(id="your-room")],
               "lodging_handoff_sources":[source]}
    if failure == "unknown":
        source["id"] = "different"
    elif failure == "swapped":
        source["own_room_id"], source["other_room_id"] = source["other_room_id"], source["own_room_id"]
    elif failure == "journey":
        source["journey_id"] = "other-trip"
    result, _ = preview(choice, context=context)
    assert (result.route_kind == "social_choice") is (failure is None)
    if failure is None:
        assert result.separate_stay_choice.source_lodging_plan_id == "paid-source"


def test_completed_source_context_is_shared_compact_and_disappears_after_new_invitation(town):
    e = town
    pid, jid, own, other = prepared(e)
    with e.db.read() as c:
        sources = LodgingPlanService.handoff_sources(c,e.wid,e.player,e.npc,current_time(e))
        assert len(sources) == 1 and sources[0]["id"] == pid
        assert {own,other} == {sources[0]["own_room_id"],sources[0]["other_room_id"]}
        assert not {"spent","quote","quote_id","private_reason","spending_limit"} & sources[0].keys()
    result = post(e,body(e,proposal(e,pid,jid,own,other)))
    assert result.status_code == 200, result.text
    with e.db.read() as c:
        assert LodgingPlanService.handoff_sources(c,e.wid,e.player,e.npc,current_time(e)) == []


def test_api_replays_explicit_linked_source_and_does_not_accept_a_different_source(town):
    e = town
    pid,jid,own,other = prepared(e)
    payload = body(e,proposal(e,pid,jid,own,other))
    first = post(e,payload)
    assert first.status_code == 200, first.text
    assert post(e,payload).json() == first.json()
    altered = copy.deepcopy(payload)
    altered["separate_stay_choice"]["source_lodging_plan_id"] = str(uuid4())
    assert post(e,altered).status_code == 409
    rows = e.client.get(f"/api/worlds/{e.wid}/separate-stays").json()
    assert rows[0]["source_lodging_plan_id"] == pid
    assert rows[0]["status"] == "pending"


def test_old_separate_stay_receipt_without_nullable_source_remains_replayable(town):
    e = town
    pid,jid,own,other = prepared(e)
    payload = body(e,proposal(e,pid,jid,own,other))
    payload["separate_stay_choice"].pop("source_lodging_plan_id")
    first = post(e,payload)
    assert first.status_code == 200,first.text
    legacy = NaturalSocialChoice.model_validate(payload).model_dump(mode="json")
    legacy["separate_stay_choice"].pop("source_lodging_plan_id")
    digest = hashlib.sha256(json.dumps(legacy,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    with e.db.write() as c:
        c.execute("UPDATE social_choice_receipts SET payload_hash=? WHERE request_id=?",(digest,payload["request_id"]))
    assert post(e,payload).json() == first.json()


def test_source_and_current_linked_commitment_stay_inside_required_prompt_budget():
    source = dict(id="source",journey_id="trip",proposer_id="npc",recipient_id="player",
        own_room_id="west",other_room_id="east",required_until_at="2030-01-04T10:00:00Z",completion_event_id="real-event")
    current = dict(id="stay",status="pending",source_lodging_plan_id="source",source_lodging_plan={
        "id":"source","required_until_at":source["required_until_at"],"completion_event_id":"real-event"})
    context = {"player_text":"我另提各自分房休息。", "intimacy":{"lodging_handoff_sources":[source],"separate_stays":[current]}}
    before=copy.deepcopy(context)
    prompt=build_npc_prompt("规则",context,max_input_tokens=1500)
    data=json.loads(prompt.messages[1]["content"])
    assert data["intimacy"]["lodging_handoff_sources"] == [source]
    assert data["intimacy"]["separate_stays"] == [current]
    assert context == before
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt("规"*1500,context,max_input_tokens=1500)


@pytest.mark.parametrize("phase", ["source", "invitation", "reunion", "finished"])
def test_handoff_limits_only_optional_history_and_preserves_original_hard_limit(phase):
    intimacy = {"lodging_handoff_sources":[{"id":"source"}]} if phase == "source" else {
        "separate_stays":[dict(id="stay",source_lodging_plan_id="source",
            status="pending" if phase == "invitation" else "completed",
            reunion_status="waiting" if phase == "reunion" else "met")]
    }
    context = {"player_text":"我们另行确认分房和会合。", "intimacy":intimacy,
               "open_commitments":[{"title":"这是不影响本次选择的旧资料。"*200}]}
    before=copy.deepcopy(context)
    prompt=build_npc_prompt("规"*6500,context)
    assert prompt.trace["optional_budget"]["limit"] == (8192 if phase == "finished" else 7400)
    assert json.loads(prompt.messages[1]["content"])["intimacy"] == intimacy
    assert context == before
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt("规"*8192,context)
