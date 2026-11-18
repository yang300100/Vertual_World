"""哥哥，只约束订房时可选历史的填充，当前两房、预算和准确报价不得裁掉。"""

import copy
import json

import pytest

from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt


def active_plan():
    return {
        "id": "plan",
        "status": "accepted",
        "roles": [
            {
                "character_id": "npc",
                "room_id": "mine",
                "spending_limit": 18,
                "quote": {"id": "exact-quote", "total": 8, "status": "open"},
            },
            {
                "character_id": "player",
                "room_id": "yours",
                "spending_limit": 12,
                "status": "unreported",
            },
        ],
    }


@pytest.mark.parametrize("source", ["flat", "nested", "history", "old_appointment", "example"])
def test_all_optional_branches_obey_booking_headroom_and_keep_legacy_budget(source):
    long_text = "这是可以按预算补充的旧资料。" * 95
    context = {
        "player_text": "这次订房仍按本人预算办理。",
        "intimacy": {"lodging_plans": [active_plan()]},
    }
    if source == "flat":
        context["open_commitments"] = [{"title": long_text}]
        trace_key = "open_commitments:0"
    elif source == "nested":
        context["intimacy"]["ongoing_considerations"] = [
            {"meaning": "本人当前顾虑。"},
            {"meaning": long_text},
        ]
        trace_key = "intimacy.ongoing_considerations:1"
    elif source == "history":
        context["recent_conversation"] = [
            "[2030-01-01T00:00:00Z] 玩家：" + long_text,
            "[2030-01-01T00:00:01Z] NPC：我记得。",
        ]
        trace_key = "recent_conversation:0"
    elif source == "old_appointment":
        context["intimacy"]["appointments"] = {
            "arrangements": [{"id": "old", "status": "completed", "statement": long_text}]
        }
        trace_key = "intimacy.appointments.arrangements:0"
    else:
        context["npc_card"] = {"dialogue_examples": [long_text]}
        trace_key = "example:0"
    before = copy.deepcopy(context)
    booking = build_npc_prompt("规" * 6500, context, max_input_tokens=8192)
    assert booking.trace["optional_budget"]["limit"] == 7400
    assert booking.input_tokens <= 7400
    assert not booking.trace[trace_key]["included"]
    assert json.loads(booking.messages[1]["content"])["intimacy"]["lodging_plans"] == [
        active_plan()
    ]
    assert before == context
    legacy = copy.deepcopy(context)
    legacy["intimacy"]["lodging_plans"] = []
    old = build_npc_prompt("规" * 6500, legacy, max_input_tokens=8192)
    assert old.trace["optional_budget"]["limit"] == 8192
    assert old.trace[trace_key]["included"]
    assert 7400 < old.input_tokens <= 8192


def test_current_booking_above_optional_target_keeps_all_required_facts():
    context = {
        "player_text": "确认的是自己的准确报价。",
        "intimacy": {"lodging_plans": [active_plan()]},
        "open_commitments": [{"title": "旧资料" * 300}],
    }
    before = copy.deepcopy(context)
    prompt = build_npc_prompt("规" * 7450, context, max_input_tokens=8192)
    mandatory = prompt.trace["mandatory"]["tokens"]
    assert 7400 < mandatory <= 8192
    assert prompt.trace["optional_budget"]["limit"] == mandatory
    assert prompt.input_tokens == mandatory
    assert json.loads(prompt.messages[1]["content"])["intimacy"]["lodging_plans"] == [active_plan()]
    assert context == before


def test_booking_headroom_never_expands_the_original_required_limit():
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt(
            "规" * 8192, {"intimacy": {"lodging_plans": [active_plan()]}}, max_input_tokens=8192
        )
