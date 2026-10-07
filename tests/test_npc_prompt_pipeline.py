"""NPC 消息管线的预算、来源与请求参数契约。"""

import json

import pytest
from pydantic import TypeAdapter

from world_engine.agent_llm import AgentModelBackend
from world_engine.config import Settings
from world_engine.decisions import NpcReply
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import (
    NpcPromptOverflow,
    SamplingIntent,
    build_deepseek_roleplay_payload,
    build_npc_prompt,
)


def _tokens(messages):
    return sum(len(item["content"]) for item in messages)


def test_mandatory_prompt_overflow_is_explicit():
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt("规则", {"npc": {"name": "守卫"}, "player_text": "问" * 100},
                         max_input_tokens=30, token_counter=_tokens)


def test_default_budget_accepts_a_rich_bound_character_card(tmp_path):
    settings = Settings(database_path=tmp_path / "world.db")
    context = {
        "npc": {"name": "星绘", "identity": "观星师", "traits": ["谨慎"]},
        "player": {"name": "旅人"},
        "npc_card": {
            "public_role": "观星师",
            "sillytavern_card": {
                "description": "记录星图与旅途见闻。" * 110,
                "personality": "沉着、谨慎，愿意认真答复。" * 80,
            },
        },
        "knowledge_context": [{"audience": "guardrail", "content": "遵守当前世界事实。" * 70}],
        "scene": {"location": {"name": "观星台"}, "nearby_characters": [
            {"name": "路人", "identity": "旅行者"} for _ in range(8)
        ]},
        "player_text": "你好呀",
    }

    prompt = build_npc_prompt(
        npc_reply_system_prompt(), context,
        max_input_tokens=settings.dialogue_context_max_tokens,
    )

    assert prompt.input_tokens <= settings.dialogue_context_max_tokens
    assert any("记录星图与旅途见闻" in item["content"] for item in prompt.messages)
    assert any("你好呀" in item["content"] for item in prompt.messages)


def test_default_budget_keeps_recent_exchange_with_long_worldbook_profile(tmp_path):
    settings = Settings(database_path=tmp_path / "world.db")
    context = {
        "npc": {"name": "观星师", "identity": "记录员"},
        "player": {"name": "旅人"},
        "npc_card": {"sillytavern_card": {
            "description": "她长期记录航线和潮汐。" * 330,
            "personality": "谨慎地核对消息。" * 70,
        }},
        "knowledge_context": [{"audience": "guardrail", "content": "不得改变已裁定的世界事实。" * 70}],
        "recent_conversation": [
            "[2026-09-29T10:00:00+00:00] 玩家：你昨天记下了什么？",
            "[2026-09-29T10:00:01+00:00] NPC：我只记下亲眼看到的潮位。",
        ],
        "player_text": "那今天呢？",
    }

    prompt = build_npc_prompt(
        npc_reply_system_prompt(), context,
        max_input_tokens=settings.dialogue_context_max_tokens,
    )

    assert prompt.input_tokens <= settings.dialogue_context_max_tokens
    assert any("你昨天记下了什么" in item["content"] for item in prompt.messages)


def test_past_scene_and_personal_experience_do_not_overflow_current_dialogue():
    context = {
        "npc": {"name": "守卫"},
        "player_text": "你好",
        "scene": {
            "location": {"name": "城门"},
            "doors": [{"id": "gate-1", "open": False}],
            "recent_personal_events": [{"summary": "旧事件" * 100}],
        },
        "personal_experience": {
            "emotion": {"mood": "平静"},
            "known_events": [{"summary": "旧传闻" * 100}],
            "beliefs": [{"summary": "旧判断" * 100}],
        },
    }
    prompt = build_npc_prompt(
        "规则", context, max_input_tokens=500, token_counter=_tokens,
    )
    joined = " ".join(item["content"] for item in prompt.messages)
    assert prompt.input_tokens <= 500
    assert "城门" in joined and "gate-1" not in joined and "你好" in joined
    assert "旧事件" not in joined and "旧传闻" not in joined and "旧判断" not in joined
    assert prompt.trace["scene.recent_personal_events:0"]["included"] is False
    assert prompt.trace["personal_experience.known_events:0"]["included"] is False


def test_history_fits_newest_complete_exchange_without_mutating_context():
    context = {
        "npc": {"name": "守卫"}, "player_text": "继续？",
        "recent_conversation": [
            "[2026-09-07T10:00:00+00:00] 玩家：旧问题" + "旧" * 80,
            "[2026-09-07T10:00:00+00:00] NPC：旧答案" + "旧" * 80,
            "[2026-09-07T10:01:00+00:00] 玩家：新问题",
            "[2026-09-07T10:01:00+00:00] NPC：新答案",
        ],
    }
    full = build_npc_prompt("规则", context, max_input_tokens=10000,
                            token_counter=_tokens)
    limit = _tokens(full.messages) - 100
    selected = build_npc_prompt("规则", context, max_input_tokens=limit,
                                token_counter=_tokens)
    contents = " ".join(item["content"] for item in selected.messages)
    assert "新问题" in contents and "新答案" in contents
    assert "旧问题" not in contents and "旧答案" not in contents
    assert context["recent_conversation"][0].startswith("[2026")
    assert selected.trace["recent_conversation"]["included"] is False


def test_examples_are_optional_and_not_treated_as_real_history():
    context = {
        "npc": {"name": "守卫"},
        "npc_card": {"dialogue_examples": ["示例语气：先问来意"]},
        "player_text": "有空吗？",
    }
    full = build_npc_prompt("规则", context, max_input_tokens=10000,
                            token_counter=_tokens)
    assert any("示例语气" in item["content"] for item in full.messages)
    assert not any(item["role"] == "assistant" and "示例语气" in item["content"]
                   for item in full.messages)
    short = build_npc_prompt("规则", context,
                             max_input_tokens=_tokens(full.messages) - 1,
                             token_counter=_tokens)
    assert not any("示例语气" in item["content"] for item in short.messages)


def test_example_precedes_real_history_in_final_messages():
    prompt = build_npc_prompt("规则", {
        "npc_card": {"dialogue_examples": ["示范语气"]},
        "recent_conversation": [
            "[2026-09-07T10:00:00+00:00] 玩家：真实问题",
            "[2026-09-07T10:00:00+00:00] NPC：真实回答",
        ],
        "player_text": "继续",
    }, max_input_tokens=10000, token_counter=_tokens)
    contents = [item["content"] for item in prompt.messages]
    assert next(i for i, text in enumerate(contents) if "示范语气" in text) < next(
        i for i, text in enumerate(contents) if "真实问题" in text
    )


def test_latest_exchange_has_budget_priority_over_optional_world_knowledge():
    knowledge = {"audience": "character_common", "content": "城门规则" * 40}
    context = {
        "player_text": "能进城吗？", "knowledge_context": [knowledge],
        "recent_conversation": [
            "[2026-09-07T10:00:00+00:00] 玩家：你好",
            "[2026-09-07T10:00:00+00:00] NPC：你好",
        ],
    }
    knowledge_only = build_npc_prompt("规则", {
        "player_text": "能进城吗？", "knowledge_context": [knowledge],
    }, max_input_tokens=10000, token_counter=_tokens)
    selected = build_npc_prompt("规则", context,
                                max_input_tokens=knowledge_only.input_tokens,
                                token_counter=_tokens)
    contents = " ".join(item["content"] for item in selected.messages)
    assert "城门规则" not in contents
    assert "你好" in contents


def test_deepseek_adapter_only_serializes_supported_sampling_fields():
    prompt = build_npc_prompt("规则", {"player_text": "你好"},
                              max_input_tokens=10000, token_counter=_tokens)
    payload = build_deepseek_roleplay_payload(
        prompt, "deepseek-chat",
        SamplingIntent(max_output_tokens=700, temperature=0.8, top_p=0.9,
                       top_k=40, stop=("结束",)),
    )
    assert payload["max_tokens"] == 700
    assert payload["temperature"] == 0.8
    assert payload["top_p"] == 0.9
    assert payload["stop"] == ["结束"]
    assert "top_k" not in payload
    assert "response_format" not in payload
    assert json.dumps(payload, ensure_ascii=False)


def test_reasoning_model_omits_unsupported_sampling():
    prompt = build_npc_prompt("规则", {"player_text": "你好"},
                              max_input_tokens=10000, token_counter=_tokens)
    payload = build_deepseek_roleplay_payload(
        prompt, "deepseek-reasoner",
        SamplingIntent(max_output_tokens=500, temperature=1.9, top_p=0.1,
                       frequency_penalty=1.0, presence_penalty=1.0),
    )
    assert set(payload) == {"model", "messages", "max_tokens", "stream"}


def test_optional_trace_reports_each_segment_cost():
    context = {"player_text": "还记得旧约定吗？", "recent_private_memories": [{"summary": "记得旧约定"}]}
    prompt = build_npc_prompt("规则", context, max_input_tokens=10000,
                              token_counter=_tokens)
    segment = prompt.trace["recent_private_memories:0"]
    assert segment["included"] is True
    assert segment["tokens"] > 0
    assert prompt.input_tokens == _tokens(prompt.messages)


def test_deepseek_adapter_clamps_sampling_to_provider_range():
    prompt = build_npc_prompt("规则", {"player_text": "你好"},
                              max_input_tokens=10000, token_counter=_tokens)
    payload = build_deepseek_roleplay_payload(
        prompt, "deepseek-chat",
        SamplingIntent(max_output_tokens=500, temperature=2.8, top_p=-0.2),
    )
    assert payload["temperature"] == 2.0
    assert payload["top_p"] == 0.0


def test_emotional_dialogue_omits_unrelated_scene_and_duplicate_events():
    context = {
        "npc": {"id": "npc", "name": "小铃"},
        "player": {"id": "player", "name": "玩家"},
        "scene": {
            "location": {"id": "town", "name": "小镇", "resources": {"food": 97}},
            "nearby_characters": [{"name": "路人甲"}],
            "inventory": [{"id": "item-1"}],
            "travel_destinations": [{"id": "place-1"}],
            "recent_personal_events": [
                {"id": "event-1", "event_type": "action.socialize", "summary": "玩家说我喜欢你，小铃表示关心"},
            ],
        },
        "personal_experience": {
            "known_events": [
                {"id": "event-1", "summary": "玩家说我喜欢你，小铃表示关心"},
                {"id": "event-2", "summary": "尚未认识的人开始休息"},
            ],
        },
        "knowledge_context": [
            {"audience": "guardrail", "content": "不得泄露隐藏事实"},
            {"audience": "character_common", "content": "王国有一座城门"},
        ],
        "recent_conversation": [
            "[2026-09-29T23:00:00+00:00] 玩家：我喜欢你",
            "[2026-09-29T23:00:01+00:00] NPC：你又来了",
        ],
        "player_text": "你这是在关心我吗？",
    }

    prompt = build_npc_prompt("世界规则", context, max_input_tokens=10000)
    background = json.loads(prompt.messages[1]["content"])

    assert sum(message["role"] == "system" for message in prompt.messages) == 1
    assert "resources" not in background["scene"]["location"]
    assert background["scene"].get("nearby_characters") == []
    assert "inventory" not in background["scene"]
    assert "travel_destinations" not in background["scene"]
    assert "recent_personal_events" not in background["scene"]
    assert "known_events" not in background.get("personal_experience", {})
    assert len(background["knowledge_context"]) == 1
    assert prompt.messages[-1]["content"].endswith("玩家输入原文：\n你这是在关心我吗？")


def test_action_request_keeps_valid_targets_for_npc_intent():
    prompt = build_npc_prompt("规则", {
        "scene": {
            "location": {"id": "town", "name": "小镇"},
            "travel_destinations": [{"id": "place-1", "name": "图书馆"}],
            "inventory": [{"id": "item-1", "name": "书"}],
        },
        "player_text": "请把书给我，然后带我前往图书馆",
    }, max_input_tokens=10000)
    scene = json.loads(prompt.messages[1]["content"])["scene"]

    assert scene["inventory"][0]["id"] == "item-1"
    assert scene["travel_destinations"][0]["id"] == "place-1"


def test_agent_roleplay_uses_final_message_budget(settings):
    from dataclasses import replace

    class Client:
        def __init__(self):
            self.requests = []

        def post(self, path, json):
            self.requests.append(json)

    client = Client()
    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="test", dialogue_context_max_tokens=800),
        client=client,
    )
    with pytest.raises(NpcPromptOverflow):
        backend.complete(system_prompt="规则", user_payload={"player_text": "问" * 2000},
                         schema=TypeAdapter(NpcReply),
                         label="letter_reply", roleplay=True)
    assert client.requests == []


def test_agent_structured_extraction_can_limit_output_tokens(settings):
    from dataclasses import replace

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"fragments":[]}'}}]}

    class Client:
        def __init__(self):
            self.payload = None

        def post(self, path, json):
            self.payload = json
            return Response()

    client = Client()
    backend = AgentModelBackend(replace(settings, deepseek_api_key="test"), client=client)

    result = backend.complete(
        system_prompt="只输出 JSON", user_payload={"selected_person": "阿月"},
        schema=TypeAdapter(dict), label="character_book_extract",
        max_output_tokens=1600,
    )

    assert result.data == {"fragments": []}
    assert client.payload["max_tokens"] == 1600
