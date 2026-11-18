"""哥哥，当前共乘、个人资料和输入硬门禁需要一起成立。"""

# ruff: noqa: F811
import copy
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import TypeAdapter
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_ride_text_api import post, proposal, ride_world  # noqa: F401

from world_engine.agent_llm import AgentModelBackend
from world_engine.engine import WorldEngine
from world_engine.outreach import OutreachText
from world_engine.roleplay import npc_reply_system_prompt, scoped_npc_protocols
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt


def current_ride(**changes):
    return {"id": "current-ride", "status": "pending", "vehicle_id": "real-vehicle", "controller_id": "player",
        "origin_location_id": "plaza", "destination_location_id": "garden", "capacity": 3,
        "deadline_at": "2030-01-01T10:00:00+00:00", "statement": "仅按原名单到已知花园，途中请求离开时先安全靠站。",
        "members": [{"character_id": cid, "decision": "accepted" if cid == "player" else "pending",
            "decision_event_id": "original-event" if cid == "player" else None, "boarded_at": None}
            for cid in ("player", "npc", "third")], **changes}


def test_actual_pending_ride_keeps_complete_terms_and_npc_personal_context(ride_world):
    e = ride_world
    response = post(e, proposal(e))
    assert response.status_code == 200, response.text
    engine = WorldEngine(e.db, e.settings, e.model)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player), player_text="请按自己的真实情况决定是否共乘，之后是否登车仍由你另行决定。",
                conversation=None, channel="in_person", interaction="本人独立回应原完整共乘")
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context)
        packed = json.loads(prompt.messages[1]["content"])
        assert prompt.input_tokens <= 8192
        assert packed["intimacy"]["rides"] == context["intimacy"]["rides"]
        assert packed["intimacy"]["own_perspective"] == context["intimacy"]["own_perspective"]
        assert "ride_intent" in prompt.messages[0]["content"]
        assert "接受不等于登车" in prompt.messages[0]["content"]
        assert "关系确认不授予独占" in prompt.messages[0]["content"]
        assert "intimacy.overnights是旅途长时住宿" not in prompt.messages[0]["content"]
        assert context == before
    finally:
        engine.close()


def test_current_ride_and_personal_preference_survive_large_optional_history():
    ride = current_ride()
    preference = {"id": "registered-belief", "content": "我更喜欢自己决定是否上车，答应聊天并不代表同意登乘。", "provenance": {
        "knowledge_level": "character_belief", "author_character_id": "npc", "source_event_id": "registered-belief"}}
    context = {"player_text": "请按你自己的意愿决定。", "character_influences": [preference],
        "intimacy": {"rides": [ride], "own_perspective": [{"concern": "我想保留途中安全下车的边界。"}]},
        "recent_private_memories": [{"summary": "早先不相干的交谈。" * 6000}]}
    before = copy.deepcopy(context)
    prompt = build_npc_prompt(npc_reply_system_prompt(), context)
    packed = json.loads(prompt.messages[1]["content"])
    assert packed["intimacy"]["rides"] == [ride]
    assert packed["character_influences"] == [preference]
    assert packed["intimacy"]["own_perspective"] == context["intimacy"]["own_perspective"]
    assert prompt.input_tokens <= 8192 and context == before


def test_required_ride_overflow_is_rejected_without_truncating_members_or_terms():
    context = {"intimacy": {"rides": [current_ride(statement="完整原约定不能裁剪" * 2600)]}}
    before = copy.deepcopy(context)
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt(npc_reply_system_prompt(), context)
    assert context == before


def test_empty_vehicle_candidates_do_not_enable_an_inapplicable_ride_protocol():
    system, _ = scoped_npc_protocols(npc_reply_system_prompt(), {"intimacy": {"rides": [], "ride_options": {"vehicles": []}}})
    assert "# 共乘" not in system


@pytest.mark.parametrize("key", ["journeys", "overnights", "separate_stays", "lodging_plans"])
def test_current_pair_arrangements_keep_all_protocols_with_current_ride(key):
    context = {"intimacy": {"rides": [current_ride()], key: [{"id": "old-current", "status": "paused"}]}}
    system, _ = scoped_npc_protocols(npc_reply_system_prompt(), context)
    assert "# 共乘" in system
    assert "intimacy.overnights是旅途长时住宿" in system
    assert "访问许可仅在本次时间窗" in system


def test_historical_ride_does_not_displace_current_ride():
    current = current_ride()
    old = current_ride(id="ended-ride", status="ended", statement="已结束的旧历史" * 2500)
    context = {"intimacy": {"rides": [old, current]}}
    prompt = build_npc_prompt(npc_reply_system_prompt(), context)
    packed = json.loads(prompt.messages[1]["content"])
    assert packed["intimacy"]["rides"] == [current]
    assert prompt.input_tokens <= 8192


def test_actual_outreach_backend_keeps_typed_decline_within_npc_budget(settings):
    requests = []
    raw = {"should_contact": True, "reply": "这一次我不参加共乘。", "ride_intent": {
        "operation": "decline", "ride_id": "current-ride", "statement": "我本人婉拒这次原共乘。"}}
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw, ensure_ascii=False)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test/") as client:
        backend = AgentModelBackend(replace(settings, deepseek_api_key="test", deepseek_max_retries=0), client)
        result = backend.complete(label="npc_outreach", system_prompt=npc_reply_system_prompt(),
            user_payload={"player_text": "", "intimacy": {"rides": [current_ride()]}},
            schema=TypeAdapter(OutreachText), roleplay=True)
    assert result.data.ride_intent.operation == "decline" and result.data.should_contact
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["max_tokens"] <= 1200
