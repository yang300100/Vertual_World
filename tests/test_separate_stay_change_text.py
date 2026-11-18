"""哥哥，改期的文字、角色和旧回执都要保留各自明确选择。"""

# ruff: noqa: F811
import copy
import hashlib
import json
from datetime import timedelta

import pytest
from test_lived_world import town  # noqa: F401
from test_separate_stays import accept, choose, get, setup
from test_separate_stays_api import body, post
from test_separate_stays_text import preview

from world_engine.api_intimacy import NaturalSocialChoice
from world_engine.repository import from_iso, to_iso
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt
from world_engine.separate_stays import SeparateStayIntent


@pytest.mark.parametrize("operation", ["accept_change", "decline_change", "withdraw_change"])
@pytest.mark.parametrize("failure", [None, "unknown", "other_person", "terminal", "forged_words"])
def test_parser_only_routes_current_change_and_the_right_person_choice(operation, failure):
    text = "这是我本人对这份分房改期的明确选择。"
    change = dict(id="change",status="pending",proposer_id="player" if operation == "withdraw_change" else "npc",
                  recipient_id="npc" if operation == "withdraw_change" else "player")
    if failure == "unknown":
        change["id"] = "unrelated-change"
    elif failure == "other_person":
        change["proposer_id"],change["recipient_id"] = change["recipient_id"],change["proposer_id"]
    elif failure == "terminal":
        change["status"] = "declined"
    choice = SeparateStayIntent(operation=operation,stay_id="stay",change_id="change",statement=text)
    result,_ = preview(choice,text=text,evidence="我没有说过这个。" if failure == "forged_words" else text,
        context={"separate_stays":[dict(id="stay",status="accepted",reschedule_requests=[change])]})
    assert (result.route_kind == "social_choice") is (failure is None)
    if failure is None:
        assert result.separate_stay_choice.change_id == "change"


@pytest.mark.parametrize("can_reschedule", [True,False])
def test_parser_new_time_is_only_a_proposal_while_old_stay_is_changeable(can_reschedule):
    choice = SeparateStayIntent(operation="reschedule",stay_id="stay",arrival_world_time="2030-01-01T23:00:00Z",
        statement="我提议把原分房入住开始改到今晚十一点，其余原约不变，请你独立考虑。")
    result,_ = preview(choice,context={"separate_stays":[dict(id="stay",status="accepted",can_reschedule=can_reschedule)]})
    assert (result.route_kind == "social_choice") is can_reschedule


@pytest.mark.parametrize("operation", ["reschedule", "accept_change", "decline_change", "withdraw_change"])
def test_apart_old_arrangement_only_allows_withdrawing_person_own_proposal(operation):
    fields={"arrival_world_time":"2030-01-01T23:00:00Z"} if operation=="reschedule" else {"change_id":"change"}
    choice=SeparateStayIntent(operation=operation,stay_id="stay",statement="我本人明确选择这次操作。",**fields)
    change=dict(id="change",status="pending",proposer_id="player",recipient_id="npc")
    result,_=preview(choice,apart=True,context={"separate_stay_only":True,"separate_stays":[dict(
        id="stay",status="accepted",can_reschedule=True,reschedule_requests=[change])]})
    assert (result.route_kind=="social_choice") is (operation=="withdraw_change")


def test_api_change_replay_keeps_old_times_until_the_other_person_accepts(town):
    e=town
    _,_,_,fields=setup(e)
    sid=accept(e,fields)
    before=get(e,sid)
    payload=body(e,dict(operation="reschedule",stay_id=sid,
        arrival_world_time=to_iso(from_iso(before["arrival_starts_at"])+timedelta(hours=1)),
        statement="我希望原入住开始晚一小时，其他房间、完整休息及会合时长都不变。"))
    first=post(e,payload)
    assert first.status_code==200,first.text
    result=first.json()
    assert result["status"]=="pending" and result["change_id"]
    assert post(e,payload).json()==result
    assert get(e,sid)==before
    own_accept=body(e,dict(operation="accept_change",stay_id=sid,change_id=result["change_id"],statement="我同意这个改期。"))
    assert post(e,own_accept).status_code==409
    assert get(e,sid)==before
    choose(e,"accept_change",sid,npc=True,change_id=result["change_id"])
    after=get(e,sid)
    assert from_iso(after["arrival_starts_at"])-from_iso(before["arrival_starts_at"])==timedelta(hours=1)
    assert all(after[k]==before[k] for k in ("minutes","source_lodging_plan_id","own_room_id","other_room_id","statement"))
    public=e.client.get(f"/api/worlds/{e.wid}/separate-stays").json()[0]
    assert public["reschedule_requests"][0]["status"]=="accepted"


@pytest.mark.parametrize("fields_to_remove", [("change_id",),("change_id","source_lodging_plan_id")])
def test_old_receipt_without_new_nullable_fields_is_still_exactly_replayed(town,fields_to_remove):
    e=town
    _,_,_,fields=setup(e)
    payload=body(e,fields)
    first=post(e,payload)
    assert first.status_code==200,first.text
    legacy=NaturalSocialChoice.model_validate(payload).model_dump(mode="json")
    for key in fields_to_remove:
        legacy["separate_stay_choice"].pop(key)
    digest=hashlib.sha256(json.dumps(legacy,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    with e.db.write() as c:
        c.execute("UPDATE social_choice_receipts SET payload_hash=? WHERE request_id=?",(digest,payload["request_id"]))
    assert post(e,payload).json()==first.json()


def test_pending_change_keeps_both_current_and_proposed_terms_in_required_prompt():
    change=dict(id="change",status="pending",proposer_id="player",recipient_id="npc",
                new_arrival_starts_at="2030-01-01T23:00:00Z",new_latest_end_at="2030-01-02T08:00:00Z")
    stay=dict(id="stay",status="accepted",arrival_starts_at="2030-01-01T22:00:00Z",minutes=480,
              own_room_id="east",other_room_id="west",reschedule_requests=[change])
    context={"player_text":"你愿意考虑这个改期吗？","intimacy":{"separate_stays":[stay]},
             "open_commitments":[{"title":"可选的旧记录。"*250}]}
    before=copy.deepcopy(context)
    prompt=build_npc_prompt("规"*6500,context)
    data=json.loads(prompt.messages[1]["content"])
    assert data["intimacy"]["separate_stays"]==[stay]
    assert prompt.trace["optional_budget"]["limit"]==7400
    assert context==before
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt("规"*8192,context)
