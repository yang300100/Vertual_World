"""Yuki 检查旧资料精简后，NPC 仍能承接对话、约定和有来源的认知。"""

import json
from copy import deepcopy

from world_engine.actions import ActionService
from world_engine.conversations import ConversationService
from world_engine.roleplay_pipeline import build_npc_prompt


def _background(context, **kwargs):
    return json.loads(build_npc_prompt("规则", context, **kwargs).messages[1]["content"])


def test_address_and_generic_question_do_not_recall_unrelated_material():
    context = {
        "npc": {"name": "小铃"},
        "player": {"name": "旅人"},
        "player_text": "在干什么啊小铃",
        "scene": {
            "recent_personal_events": [
                {"id": "old", "summary": "小铃收取了一份粮食。"},
            ]
        },
        "personal_experience": {
            "beliefs": [
                {"subject_name": "尚未认识的人", "predicate": "location", "value": "城镇"},
            ]
        },
        "knowledge_context": [{"audience": "character_common", "content": "小铃所在王国的旧史。"}],
        "conversation_episodes": [
            {"summary": "玩家：你好小铃\nNPC：你好", "source_event_ids": ["old"]}
        ],
        "recent_private_memories": [{"summary": "小铃去年买了一本书。", "importance": 6}],
        "recalled_past_conversation": ["[昨日] 玩家：小铃你好"],
    }
    background = _background(context)
    assert not background.get("knowledge_context")
    assert not background.get("conversation_episodes")
    assert not background.get("recent_private_memories")
    assert not background.get("recalled_past_conversation")
    assert not background.get("scene", {}).get("recent_personal_events")
    assert not background.get("personal_experience", {}).get("beliefs")


def test_relevant_belief_keeps_uncertainty_and_source_without_audit_ids():
    belief = {
        "id": "edge-a",
        "subject_id": "person-a",
        "subject_name": "阿澈",
        "predicate": "location",
        "value": "旧港",
        "status": "stale",
        "confidence": 0.4,
        "last_verified_at": "2040-04-01T12:00:00+00:00",
        "evidence": [
            {
                "method": "reported",
                "quote": "阿澈说要去旧港。",
                "learned_at": "2040-04-01T12:00:00+00:00",
            }
        ],
    }
    context = {"player_text": "阿澈在哪里？", "personal_experience": {"beliefs": [belief]}}
    original = deepcopy(context)
    kept = _background(context)["personal_experience"]["beliefs"][0]
    assert kept["status"] == "stale" and kept["confidence"] == 0.4
    assert kept["subject_name"] == "阿澈" and kept["value"] == "旧港"
    assert kept["evidence"][0]["method"] == "reported"
    assert kept["last_verified_at"] == belief["last_verified_at"]
    assert "id" not in kept and "subject_id" not in kept
    assert context == original


def test_memory_and_episode_do_not_repeat_an_included_history_exchange():
    reply = "粮袋我已经核对过了。"
    background = _background(
        {
            "player_text": "粮袋呢？",
            "recent_conversation": ["[今日] 玩家：核对粮袋了吗？", f"[今日] NPC：{reply}"],
            "recent_private_memories": [{"event_id": "e1", "summary": f"我回应“{reply}”。"}],
            "conversation_episodes": [
                {"summary": f"玩家：核对粮袋了吗？\nNPC：{reply}", "source_event_ids": ["e1"]}
            ],
            "scene": {
                "recent_personal_events": [
                    {"id": "e1", "event_type": "action.socialize", "summary": f"NPC说：{reply}"},
                ]
            },
        }
    )
    assert not background.get("recent_private_memories")
    assert not background.get("conversation_episodes")
    assert not background.get("scene", {}).get("recent_personal_events")


def test_explicit_past_recall_keeps_original_pair_instead_of_duplicate_derivatives():
    turns = ["[昨日] 玩家：明天核对粮袋好吗？", "[昨日] NPC：好，我答应明天核对粮袋。"]
    background = _background(
        {
            "player_text": "上次你答应核对粮袋时，原话是什么？",
            "recalled_past_conversation": turns,
            "recent_private_memories": [{"event_id": "e1", "summary": "我答应明天核对粮袋。"}],
            "conversation_episodes": [
                {
                    "summary": "玩家：明天核对粮袋好吗？\nNPC：好，我答应明天核对粮袋。",
                    "source_event_ids": ["e1"],
                }
            ],
        }
    )
    assert background["recalled_past_conversation"] == turns
    assert not background.get("recent_private_memories")
    assert not background.get("conversation_episodes")


def test_cross_source_event_and_repeated_occurrence_are_selected_once():
    background = _background(
        {
            "player_text": "最近收粮食了吗？",
            "scene": {
                "recent_personal_events": [
                    {
                        "id": "e1",
                        "event_type": "action.resource_harvested",
                        "summary": "收取了一份粮食。",
                    },
                    {
                        "id": "e2",
                        "event_type": "action.resource_harvested",
                        "summary": "收取了一份粮食。",
                    },
                ]
            },
            "personal_experience": {"known_events": [{"id": "e1", "summary": "收取了一份粮食。"}]},
            "recent_private_memories": [{"event_id": "e1", "summary": "收取了一份粮食。"}],
        }
    )
    assert len(background["scene"]["recent_personal_events"]) == 1
    assert not background.get("personal_experience", {}).get("known_events")
    assert not background.get("recent_private_memories")


def test_dropped_history_does_not_suppress_an_affordable_memory():
    context = {
        "player_text": "粮袋怎么样？",
        "recent_conversation": [
            "[今日] 玩家：核对粮袋" + "问" * 1000,
            "[今日] NPC：粮袋已经核对好了。" + "答" * 1000,
        ],
        "recent_private_memories": [{"summary": "粮袋已经核对好了。"}],
    }
    background = _background(context, max_input_tokens=600)
    assert background["recent_private_memories"][0]["summary"] == "粮袋已经核对好了。"


def test_long_knowledge_does_not_displace_latest_complete_exchange():
    context = {
        "player_text": "粮袋呢？",
        "recent_conversation": ["[今日] 玩家：核对了吗？", "[今日] NPC：还没有。"],
        "knowledge_context": [{"audience": "character_common", "content": "粮袋规则。" * 90}],
    }
    prompt = build_npc_prompt("规则", context, max_input_tokens=550)
    assert any(
        message["role"] == "assistant" and message["content"] == "还没有。"
        for message in prompt.messages
    )


def test_metadata_stays_out_of_relevant_knowledge_payload():
    background = _background(
        {
            "player_text": "城门何时开放？",
            "knowledge_context": [
                {
                    "id": "chunk",
                    "source": "docs/internal.md",
                    "score": 12.3,
                    "audience": "character_common",
                    "section": "城门",
                    "content": "城门在清晨开放。",
                    "world_scopes": ["世界"],
                    "region_scopes": ["地区"],
                    "valid_until": "2041-01-01T00:00:00+00:00",
                }
            ],
        }
    )
    kept = background["knowledge_context"][0]
    assert kept["content"] == "城门在清晨开放。"
    assert kept["valid_until"] == "2041-01-01T00:00:00+00:00"
    assert not {"id", "source", "score", "world_scopes", "region_scopes"} & kept.keys()


def test_pending_commitment_and_current_activity_survive_small_talk():
    background = _background(
        {
            "player_text": "你好小铃",
            "npc": {"name": "小铃"},
            "npc_current_activity": {"kind": "rest", "status": "running"},
            "open_commitments": [{"title": "核对粮袋", "details": "明天交付"}],
            "personal_experience": {
                "shared_appointments": [
                    {"status": "active", "due_world_time": "2040-04-06T10:00:00+00:00"},
                ]
            },
        }
    )
    assert background["npc_current_activity"]["status"] == "running"
    assert background["open_commitments"][0]["title"] == "核对粮袋"
    assert background["personal_experience"]["shared_appointments"][0]["status"] == "active"


def test_source_budget_does_not_let_unrelated_experience_displace_history():
    service = ConversationService(max_context_tokens=800)
    history = ["[今日] 玩家：你好呀", "[今日] NPC：你好呀"]
    context = service._apply_context_budget(
        {
            "player_text": "你好",
            "recent_conversation": history,
            "personal_experience": {"beliefs": [{"summary": "旧知识" * 1000}]},
            "scene": {"recent_personal_events": [{"summary": "旧事件" * 1000}]},
        }
    )
    assert context["recent_conversation"] == history
    assert context["budget_trace"]["over_budget_tokens"] == 0


def test_episode_excerpt_keeps_complete_relevant_exchange_and_drops_audit_metadata():
    background = _background(
        {
            "player_text": "粮袋核对了吗？",
            "conversation_episodes": [
                {
                    "summary": (
                        "玩家：你好\nNPC：你好\n玩家：粮袋核对了吗？\n"
                        "NPC：还没有核对，别当成完成了。"
                    ),
                    "source_event_ids": ["greeting", "grain"],
                    "source_hash": "a" * 64,
                    "first_turn_index": 1,
                    "last_turn_index": 4,
                    "open_questions": ["你好", "粮袋核对了吗？"],
                }
            ],
        }
    )
    episode = background["conversation_episodes"][0]
    assert episode["summary"] == "玩家：粮袋核对了吗？\nNPC：还没有核对，别当成完成了。"
    assert episode["summary_kind"] == "original_excerpt"
    assert episode["open_questions"] == ["粮袋核对了吗？"]
    assert "source_hash" not in episode and "source_event_ids" not in episode


def test_matching_quote_does_not_hide_a_new_fact_outside_the_quote():
    background = _background(
        {
            "player_text": "钥匙呢？",
            "recent_conversation": ["[今日] 玩家：钥匙带了吗？", "[今日] NPC：钥匙还在。"],
            "recent_private_memories": [
                {
                    "summary": "我回应“钥匙还在。”。后来发现钥匙丢失了。",
                }
            ],
        }
    )
    assert "后来发现钥匙丢失" in background["recent_private_memories"][0]["summary"]


def test_identical_question_with_new_reply_is_not_a_duplicate_social_event():
    background = _background(
        {
            "player_text": "钥匙呢？",
            "recent_conversation": ["[今日] 玩家：钥匙在哪里？", "[今日] NPC：在包里。"],
            "scene": {
                "recent_personal_events": [
                    {
                        "event_type": "action.socialize",
                        "summary": "玩家说“钥匙在哪里？”，NPC回应“刚发现钥匙丢失了。”。",
                    }
                ]
            },
        }
    )
    assert "钥匙丢失" in background["scene"]["recent_personal_events"][0]["summary"]


def test_source_limit_skips_a_large_relevant_item_and_keeps_a_small_one():
    prompt = build_npc_prompt(
        "规则",
        {
            "player_text": "城门规则？",
            "knowledge_context": [
                {"audience": "character_common", "content": "城门规则。" * 400},
                {"audience": "character_common", "content": "城门在清晨开放。"},
            ],
        },
    )
    background = json.loads(prompt.messages[1]["content"])
    assert [item["content"] for item in background["knowledge_context"]] == ["城门在清晨开放。"]
    assert prompt.trace["knowledge_context:0"]["reason"] == "source_budget"


def test_upcoming_appointment_and_commitment_are_not_displaced_by_old_history():
    context = {
        "player_text": "继续",
        "open_commitments": [{"title": "明天核对粮袋"}],
        "personal_experience": {
            "shared_appointments": [
                {"status": "active", "due_world_time": "2040-04-06T10:00:00+00:00"}
            ]
        },
        "recent_conversation": [
            turn
            for index in range(10)
            for turn in (
                f"[今日] 玩家：第{index}轮，我们核对粮袋吗？",
                f"[今日] NPC：第{index}轮，明天再核对粮袋。",
            )
        ],
    }
    prompt = build_npc_prompt("规则", context, max_input_tokens=420)
    background = json.loads(prompt.messages[1]["content"])
    assert background["open_commitments"][0]["title"] == "明天核对粮袋"
    assert background["personal_experience"]["shared_appointments"][0]["status"] == "active"
    assert any(
        item["role"] == "assistant" and "第9轮" in item["content"] for item in prompt.messages
    )


def test_important_private_memory_can_affect_small_talk_without_topic_match():
    background = _background(
        {
            "player_text": "你好",
            "recent_private_memories": [
                {"summary": "昨天受伤了，今天仍然疼痛。", "importance": 9, "confidence": 1},
            ],
        }
    )
    assert background["recent_private_memories"][0]["importance"] == 9


def test_source_budget_preserves_current_commitments_before_long_history():
    history = [
        turn
        for index in range(40)
        for turn in (
            f"[今日] 玩家：第{index}轮，我们核对粮袋吗？",
            f"[今日] NPC：第{index}轮，明天再核对粮袋。",
        )
    ]
    context = ConversationService(max_context_tokens=800)._apply_context_budget(
        {
            "player_text": "继续",
            "recent_conversation": history,
            "open_commitments": [
                {
                    "title": "明天核对粮袋",
                    "details": "核对库存数量和损耗，记录差异，并向旅人交付结果。" * 4,
                }
            ],
            "personal_experience": {
                "shared_appointments": [
                    {"status": "active", "due_world_time": "2040-04-06T10:00:00+00:00"}
                ]
            },
        }
    )
    assert context["open_commitments"][0]["title"] == "明天核对粮袋"
    assert context["personal_experience"]["shared_appointments"][0]["status"] == "active"
    assert context["recent_conversation"][-2:] == history[-2:]


def test_source_budget_never_splits_a_recalled_original_exchange():
    small_pair = ["[昨日] 玩家：粮袋还在吗？", "[昨日] NPC：粮袋还在。"]
    context = ConversationService(max_context_tokens=800)._apply_context_budget(
        {
            "player_text": "上次粮袋的原话是什么？",
            "recalled_past_conversation": [
                "[昨日] 玩家：粮袋核对了吗？",
                "[昨日] NPC：粮袋还没有核对。" + "详细说明。" * 190,
                *small_pair,
            ],
        }
    )
    assert context["recalled_past_conversation"] == small_pair


def test_two_stage_budget_does_not_let_unrelated_knowledge_hide_relevant_news():
    context = ConversationService(max_context_tokens=800)._apply_context_budget(
        {
            "player_text": "城门开放吗？",
            "knowledge_context": [{"audience": "character_common", "content": "历史资料。" * 157}],
            "personal_experience": {
                "known_events": [
                    {
                        "summary": "城门暂时封闭，守卫要求等待修复完成后再开放。" * 2,
                        "source_kind": "experienced",
                        "confidence": 1,
                    }
                ]
            },
        }
    )
    background = _background(context, max_input_tokens=800)
    assert "暂时封闭" in background["personal_experience"]["known_events"][0]["summary"]


def test_real_social_event_wrapper_is_deduplicated_against_complete_history():
    question, answer = "粮袋核对了吗？", "粮袋已经核对好了。"
    background = _background(
        {
            "player_text": "粮袋呢？",
            "recent_conversation": [f"[今日] 玩家：{question}", f"[今日] NPC：{answer}"],
            "scene": {
                "recent_personal_events": [
                    {
                        "event_type": "action.socialize",
                        "summary": ActionService.socialize_summary(
                            "旅人", "小铃", question, answer
                        ),
                    }
                ]
            },
        }
    )
    assert not background["scene"].get("recent_personal_events")


def test_historical_question_does_not_hide_a_new_actual_event():
    background = _background(
        {
            "player_text": "最近收粮食了吗？",
            "npc": {"name": "小铃"},
            "recent_conversation": [
                "[昨日] 玩家：小铃收取了一份粮食了吗？",
                "[昨日] NPC：还没有，稍后再收。",
            ],
            "scene": {
                "recent_personal_events": [
                    {
                        "id": "new",
                        "event_type": "action.resource_harvested",
                        "summary": "小铃收取了一份粮食。",
                        "occurred_at": "2040-04-06T10:00:00+00:00",
                    }
                ]
            },
        }
    )
    assert background["scene"]["recent_personal_events"][0]["summary"] == "小铃收取了一份粮食。"


def test_plain_continuation_inherits_the_previous_topic():
    for text in ("继续", "然后呢", "那今天呢？"):
        context = ConversationService(max_context_tokens=800)._apply_context_budget(
            {
                "player_text": text,
                "recent_conversation": ["[今日] 玩家：城门开放了吗？", "[今日] NPC：晚上会关门。"],
                "knowledge_context": [
                    {"audience": "character_common", "content": "城门清晨开放。"}
                ],
            }
        )
        background = _background(context)
        assert background["knowledge_context"][0]["content"] == "城门清晨开放。"


def test_new_witnessed_memory_is_not_covered_by_a_historical_question():
    background = _background(
        {
            "player_text": "粮袋核对了吗？",
            "recent_conversation": [
                "[昨日] 玩家：粮袋已经核对好了嘛？",
                "[昨日] NPC：还没有核对。",
            ],
            "recent_private_memories": [
                {"event_id": "new", "memory_type": "experienced", "summary": "粮袋已经核对好了。"}
            ],
        }
    )
    assert background["recent_private_memories"][0]["summary"] == "粮袋已经核对好了。"


def test_short_positive_reply_does_not_match_a_previous_negative_reply():
    question = "粮袋可以给吗？"
    background = _background(
        {
            "player_text": "粮袋呢？",
            "recent_conversation": [f"[昨日] 玩家：{question}", "[昨日] NPC：不好，粮袋不能给你。"],
            "scene": {
                "recent_personal_events": [
                    {
                        "event_type": "action.socialize",
                        "summary": ActionService.socialize_summary("旅人", "小铃", question, "好"),
                    }
                ]
            },
        }
    )
    assert "回应道：“好”" in background["scene"]["recent_personal_events"][0]["summary"]


def test_same_event_memory_keeps_a_distinct_private_reaction():
    background = _background(
        {
            "player_text": "粮袋呢？",
            "scene": {
                "recent_personal_events": [
                    {
                        "id": "grain",
                        "event_type": "action.resource_harvested",
                        "summary": "我收取了一份粮食，装进粮袋。",
                    }
                ]
            },
            "recent_private_memories": [
                {
                    "event_id": "grain",
                    "importance": 9,
                    "memory_type": "experienced",
                    "summary": "我收取了一份粮食，装进粮袋。想到家里余粮不足，我很担忧。",
                }
            ],
        }
    )
    assert "我很担忧" in background["recent_private_memories"][0]["summary"]


def test_two_stage_budget_skips_memory_that_the_final_source_limit_would_reject():
    context = ConversationService(max_context_tokens=800)._apply_context_budget(
        {
            "player_text": "粮袋核对了吗？",
            "recent_private_memories": [
                {
                    "summary": "粮袋核对记录。" * 90,
                    "memory_type": "experienced",
                    "importance": 6,
                    "confidence": 1,
                }
            ],
            "personal_experience": {
                "known_events": [
                    {
                        "summary": "粮袋已核对完成，守卫确认数量无误。" * 10,
                        "source_kind": "experienced",
                        "confidence": 1,
                    }
                ]
            },
        }
    )
    background = _background(context, max_input_tokens=800)
    assert "已核对完成" in background["personal_experience"]["known_events"][0]["summary"]
