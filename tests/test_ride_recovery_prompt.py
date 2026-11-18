"""哥哥，原条款、新恢复授权及本人顾虑一起留在8192门禁内。"""

# ruff: noqa: F811
import copy
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import TypeAdapter
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_ride_text_api import ride_world  # noqa: F401
from test_ride_recovery_text import propose, recovery_world  # noqa: F401
from test_ride_prompt_budget import current_ride

from world_engine.agent_llm import AgentModelBackend
from world_engine.engine import WorldEngine
from world_engine.outreach import OutreachText
from world_engine.roleplay import npc_reply_system_prompt, scoped_npc_protocols
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt, estimate_chat_tokens


def recovery(**changes):
    return {
        "id": "recovery-2", "ride_id": "current-ride", "epoch": 2, "status": "pending",
        "proposer_id": "player", "controller_id": "player", "movement_id": "original-movement",
        "deadline_at": "2030-01-01T13:00:00+00:00", "proposed_at": "2030-01-01T11:00:00+00:00",
        "terms": {"recovery_id": "recovery-2", "ride_id": "current-ride", "epoch": 2,
            "controller_id": "player", "movement_id": "original-movement", "block_event_id": "actual-block",
            "destination_location_id": "original-garden", "deadline_at": "2030-01-01T13:00:00+00:00",
            "member_ids": ["player", "npc", "third"], "remaining_seconds": 600, "remaining_distance_km": 2,
            "route_sha256": "original-route-hash", "distance_travelled_km": 1, "route_index": 1,
            "stop_requested": True, "stop_policy": "先按原路线到原安全目的地停靠，由本人决定下车。",
            "statement": "只恢复原控制者与原路线的剩余部分，原停止请求继续有效。"},
        "members": [{"character_id": cid, "decision": "accepted" if cid == "player" else "pending",
            "decision_at": "2030-01-01T11:00:00+00:00" if cid == "player" else None,
            "decision_event_id": "recovery-proposal" if cid == "player" else None} for cid in ("player", "npc", "third")],
        "source_event_id": "recovery-proposal", "can_accept": True, "can_decline": True,
        "can_withdraw": False, "can_resume": False, **changes,
    }


def context_with_recovery(plan=None):
    return {"player_text": "请本人决定是否接受本轮恢复，拒绝也可以。",
        "intimacy": {"rides": [current_ride(status="blocked", recovery=plan or recovery(),
            can_propose_recovery=False, effective_deadline_at="2030-01-01T13:00:00+00:00")]}}


def test_actual_blocked_ride_preserves_complete_recovery_and_original_terms(recovery_world):
    e = recovery_world
    recovery_id = propose(e)
    engine = WorldEngine(e.db, e.settings, e.model)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player), player_text="这次只按你的意愿决定恢复，原同意不会替代本轮选择。",
                conversation=None, channel="in_person", interaction="在实际挂起的载具内独立商量恢复")
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context)
        packed = json.loads(prompt.messages[1]["content"])
        assert packed["intimacy"]["rides"] == context["intimacy"]["rides"]
        original = context["intimacy"]["rides"][0]
        assert original["recovery"]["terms"]["recovery_id"] == recovery_id
        assert original["status"] == "blocked" and original["recovery"]["status"] == "pending"
        assert original["recovery"]["terms"]["member_ids"] == e.ids
        assert "propose_recovery" in prompt.messages[0]["content"]
        assert "accept_recovery" in prompt.messages[0]["content"] and "withdraw_recovery" in prompt.messages[0]["content"]
        assert prompt.input_tokens <= 8192 and estimate_chat_tokens(prompt.messages) <= 8192
        assert context == before
    finally:
        engine.close()


def test_current_recovery_and_private_preference_survive_large_optional_history():
    context = context_with_recovery()
    preference = {"id": "registered-recovery-belief", "content": "我希望先逐人重新确认，之前答应上车并不代表这次必须继续。",
        "provenance": {"knowledge_level": "character_belief", "author_character_id": "npc", "source_event_id": "registered-personal-belief"}}
    context["character_influences"] = [preference]
    context["intimacy"]["own_perspective"] = [{"concern": "我已经提出安全靠站，不想让旧停止请求被悄悄撤销。"}]
    context["recent_private_memories"] = [{"summary": "曾经无关的闲谈。" * 10000}]
    before = copy.deepcopy(context)
    prompt = build_npc_prompt(npc_reply_system_prompt(), context)
    packed = json.loads(prompt.messages[1]["content"])
    assert packed["intimacy"]["rides"] == context["intimacy"]["rides"]
    assert packed["character_influences"] == [preference]
    assert packed["intimacy"]["own_perspective"] == context["intimacy"]["own_perspective"]
    assert prompt.input_tokens <= 8192 and context == before


def test_required_recovery_overflow_never_truncates_roster_original_stop_or_terms():
    plan = recovery()
    plan["terms"]["statement"] = "本轮原完整恢复条款不可截断。" * 2500
    context = context_with_recovery(plan)
    before = copy.deepcopy(context)
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt(npc_reply_system_prompt(), context)
    assert context == before


def test_normal_moving_ride_does_not_add_unusable_recovery_instructions():
    context = {"intimacy": {"rides": [current_ride(status="moving", recovery=None, can_propose_recovery=False)]}}
    system, _ = scoped_npc_protocols(npc_reply_system_prompt(), context)
    assert "# 共乘" in system and "propose_recovery" not in system


@pytest.mark.parametrize("key", ["journeys", "overnights", "separate_stays", "lodging_plans"])
def test_another_real_current_arrangement_keeps_its_protocol_with_recovery(key):
    context = context_with_recovery()
    context["intimacy"][key] = [{"id": "other-real-arrangement", "status": "paused"}]
    system, _ = scoped_npc_protocols(npc_reply_system_prompt(), context)
    assert "accept_recovery" in system
    assert "intimacy.overnights是旅途长时住宿" in system and "访问许可仅在本次时间窗" in system


def test_actual_backend_preserves_typed_recovery_id_without_extra_budget_or_model_calls(settings):
    requests = []
    raw = {"should_contact": True, "reply": "这一次恢复我本人暂时不接受。", "ride_intent": {
        "operation": "decline_recovery", "ride_id": "current-ride", "recovery_id": "recovery-2",
        "statement": "我本人婉拒本轮新的恢复条款。"}}
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw, ensure_ascii=False)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test/") as client:
        backend = AgentModelBackend(replace(settings, deepseek_api_key="test", deepseek_max_retries=0), client)
        result = backend.complete(label="npc_outreach", system_prompt=npc_reply_system_prompt(),
            user_payload=context_with_recovery(), schema=TypeAdapter(OutreachText), roleplay=True)
    assert len(requests) == 1
    assert result.data.ride_intent.operation == "decline_recovery" and result.data.ride_intent.recovery_id == "recovery-2"
    assert estimate_chat_tokens(requests[0]["messages"]) <= 8192
    assert requests[0]["max_tokens"] <= 1200 and requests[0]["thinking"] == {"type": "disabled"}
