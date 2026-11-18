"""哥哥，保留完整多人条款与本人依据，未适用住宿说明按真实上下文选择。"""

# ruff: noqa: F811
import copy
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import TypeAdapter
from test_group_journey_text_api import post, proposal, trio  # noqa: F401
from test_lived_world import town  # noqa: F401

from world_engine.agent_llm import AgentModelBackend
from world_engine.engine import WorldEngine
from world_engine.outreach import OutreachText
from world_engine.roleplay import npc_reply_system_prompt, scoped_npc_protocols
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt


def test_actual_current_group_keeps_all_members_stops_and_sources_without_unused_pair_stay_protocol(trio):
    e = trio
    response = post(e, proposal(e))
    assert response.status_code == 200
    engine = WorldEngine(e.db, e.settings, e.model)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player), player_text="请按自己的真实情况决定是否参加这份完整多人行程。",
                conversation=None, channel="in_person", interaction="本人独立回应多人行程")
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context)
        assert prompt.input_tokens <= 8192
        assert prompt.trace["inactive_pair_stay_protocols"]["included"] is False
        packed = json.loads(prompt.messages[1]["content"])
        assert packed["intimacy"]["group_journeys"] == context["intimacy"]["group_journeys"]
        assert "group_journey_intent" in prompt.messages[0]["content"]
        assert "intimacy.overnights是旅途长时住宿" not in prompt.messages[0]["content"]
        assert "关系确认不授予独占" in prompt.messages[0]["content"]
        assert "访问许可仅在本次时间窗" in prompt.messages[0]["content"]
        assert context == before
    finally:
        engine.close()


@pytest.mark.parametrize("key", ["journeys", "overnights", "separate_stays", "lodging_plans"])
def test_existing_pair_journey_or_stay_never_loses_its_applicable_protocol(key):
    full = npc_reply_system_prompt()
    context = {"intimacy": {"group_journey_options": {"members": []}, key: [{"id": "current", "status": "accepted"}]}}
    selected, changed = scoped_npc_protocols(full, context)
    assert "intimacy.overnights是旅途长时住宿" in selected
    assert changed == "inactive_group"
    prompt = build_npc_prompt(full, context)
    assert "intimacy.overnights是旅途长时住宿" in prompt.messages[0]["content"]
    assert json.loads(prompt.messages[1]["content"])["intimacy"][key] == context["intimacy"][key]


@pytest.mark.parametrize("contact", [False, True])
def test_real_outreach_backend_uses_npc_budget_and_preserves_no_contact(contact, settings):
    requests = []
    raw = {"should_contact": contact, "reply": "我可以先谈谈本人的想法。" if contact else None}
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw, ensure_ascii=False)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test/") as client:
        backend = AgentModelBackend(replace(settings, deepseek_api_key="test", deepseek_max_retries=0), client)
        result = backend.complete(label="npc_outreach", system_prompt=npc_reply_system_prompt(),
            user_payload={"player_text": "", "intimacy": {"group_journey_options": {"members": []}}},
            schema=TypeAdapter(OutreachText), roleplay=True)
    assert result.data.should_contact is contact
    assert result.data.reply == raw["reply"]
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["max_tokens"] <= 1200


def test_required_group_overflow_is_still_rejected_without_truncating_contract():
    group = {"id": "group", "status": "pending", "statement": "完整承诺" * 2500}
    context = {"intimacy": {"group_journeys": [group], "group_journey_options": {"members": []}}}
    before = copy.deepcopy(context)
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt(npc_reply_system_prompt(), context)
    assert context == before


def test_both_current_group_and_pair_records_keep_all_applicable_protocols():
    full = npc_reply_system_prompt()
    context = {"intimacy": {"group_journey_options": {"members": []},
        "group_journeys": [{"id": "group", "status": "pending"}],
        "journeys": [{"id": "pair", "status": "active"}]}}
    selected, scope = scoped_npc_protocols(full, context)
    assert selected == full and scope is None


def test_old_dialogue_retelling_is_optional_but_all_current_group_terms_remain():
    group = {"id": "current-group", "status": "active", "leader_id": "npc", "deadline_at": "2030-01-01T10:00:00+00:00",
             "members": [{"character_id": name, "decision": "accepted"} for name in ("player", "npc", "third")],
             "stops": [{"location_id": "garden", "minutes": 1}], "separation_policy": "pause"}
    context = {"player_text": "我们还在按约定同行。", "intimacy": {
        "group_journeys": [group], "group_journey_options": {"members": []},
        "shared_experiences": [{"source_event_id": "past-conversation", "summary": "之前完整的交谈原文。" * 1600}],
    }}
    before = copy.deepcopy(context)
    prompt = build_npc_prompt(npc_reply_system_prompt(), context)
    packed = json.loads(prompt.messages[1]["content"])
    assert packed["intimacy"]["group_journeys"] == [group]
    assert not packed["intimacy"].get("shared_experiences")
    assert prompt.input_tokens <= 8192
    assert context == before
