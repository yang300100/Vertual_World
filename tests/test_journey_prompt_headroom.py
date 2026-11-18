"""哥哥，有限同行的准确选择要留出回答空间，既有承诺仍完整保留。"""

import copy
import json

import pytest

from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt


def travel(status):
    return {
        "id": "finite-journey", "status": status, "phase": "ready",
        "leader_id": "npc", "follow_authorized_by": None,
        "deadline_at": "2030-01-01T12:00:00+00:00", "origin_id": "workshop",
        "route": "广场(停留2分) → 工坊(停留0分)",
        "statement": "行程接受与选择跟随分开；任何一方可以退出。",
    }


@pytest.mark.parametrize("status", ["pending", "accepted", "active", "paused", "resume_pending"])
def test_live_finite_journey_keeps_whole_route_and_limits_only_optional_history(status):
    context = {
        "player_text": "请按本人处境决定是否接受或恢复这份行程。",
        "intimacy": {"journeys": [travel(status)]},
        "recent_conversation": [
            "[2030-01-01T00:00:00Z] 玩家：" + "这是可选旧聊天内容。" * 100,
            "[2030-01-01T00:00:01Z] NPC：我记得这次交谈。",
        ],
    }
    before = copy.deepcopy(context)
    prompt = build_npc_prompt("规" * 6500, context)
    assert prompt.input_tokens <= 7400
    assert prompt.trace["optional_budget"]["source"] == "journey_output_headroom"
    assert not prompt.trace["recent_conversation:0"]["included"]
    assert json.loads(prompt.messages[1]["content"])["intimacy"]["journeys"] == [travel(status)]
    assert context == before


def test_shared_motion_without_finite_journey_keeps_current_movement_facts():
    shared = {"kind": "companion", "session_id": "real-session", "in_motion": True, "has_arrived": False}
    context = {"player_text": "我们边走边聊吧。", "shared_travel": shared}
    prompt = build_npc_prompt("规则", context)
    assert prompt.trace["optional_budget"]["limit"] == 7400
    assert json.loads(prompt.messages[1]["content"])["shared_travel"] == shared


def test_required_journey_above_optional_target_keeps_terms_and_original_hard_limit():
    context = {"intimacy": {"journeys": [travel("accepted")]}}
    prompt = build_npc_prompt("规" * 7450, context)
    assert 7400 < prompt.input_tokens <= 8192
    assert prompt.input_tokens == prompt.trace["mandatory"]["tokens"]
    assert json.loads(prompt.messages[1]["content"])["intimacy"]["journeys"] == [travel("accepted")]
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt("规" * 8192, context)


def test_completed_journey_does_not_permanently_reduce_normal_conversation_budget():
    prompt = build_npc_prompt("规则", {"intimacy": {"journeys": [travel("completed")]}})
    assert prompt.trace["optional_budget"]["limit"] == 8192


def test_registered_viewpoint_keeps_source_and_uncertainty_through_both_prompt_budgets():
    from world_engine.conversations import ConversationService

    provenance = {
        "knowledge_level": "character_belief", "confidence": 0.4,
        "source_event_id": "real-source", "registration_id": "confirmed-registration",
        "author_character_id": "npc", "subject_entity_id": "local-custom",
    }
    context = {
        "npc": {"id": "npc", "name": "居民"}, "player_text": "你怎样看待同行时的当地习俗？",
        "knowledge_context": [{"id": "dynamic:belief", "audience": "character_common",
                               "section": "同行习俗", "content": "我认为同行时应先商量是否停留。",
                               "provenance": provenance}],
    }
    before = copy.deepcopy(context)
    bounded = ConversationService(max_context_tokens=1800)._apply_context_budget(context)
    prompt = build_npc_prompt("保留本人观点的来源和不确定性。", bounded, max_input_tokens=1800)
    lore = json.loads(prompt.messages[1]["content"])["knowledge_context"]
    assert lore[0]["provenance"] == provenance
    assert lore[0]["content"] == before["knowledge_context"][0]["content"]
    assert "文化规则要求所有人" not in lore[0]["content"]
