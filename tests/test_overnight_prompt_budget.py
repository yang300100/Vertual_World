"""哥哥，用真实隔离住宿上下文防止新增规则再次挤爆固定输入预算。"""

import copy
import json
from pathlib import Path

import pytest

from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import build_npc_prompt


@pytest.mark.parametrize("long_scene", [False, True])
def test_actual_pending_stay_fits_budget_without_losing_current_consent(long_scene):
    # 哥哥，样本来自只读隔离存档；长场景仅重复真实原话施加历史预算压力。
    context = json.loads(
        (Path(__file__).parent / "fixtures/overnight_pending_prompt.json").read_text(
            encoding="utf-8"
        )
    )
    if long_scene:
        context["recent_conversation"] *= 30
        context["player_text"] += (
            "我们已经谈到这次八小时住宿，是否愿意接受仍由你自己决定；"
            "房租、进入房间、正式开始休息和之后恢复旅行分别处理。"
            "我还想听听你对这次安排、到场时间、休息时长和途中照料自己有什么想法。"
        ) * 3
    before = copy.deepcopy(context)
    prompt = build_npc_prompt(npc_reply_system_prompt(), context, max_input_tokens=8192)
    sent = json.loads(prompt.messages[1]["content"])
    assert prompt.input_tokens <= 8192
    assert prompt.trace["mandatory"]["tokens"] <= 8000
    assert "本轮仍只输出一个JSON对象" in prompt.messages[0]["content"]
    assert "对应*_intent" in prompt.messages[0]["content"]
    assert "不代玩家同意或行动" in prompt.messages[0]["content"]
    assert sent["intimacy"]["overnights"] == before["intimacy"]["overnights"]
    assert sent["intimacy"]["journeys"] == before["intimacy"]["journeys"]
    # 哥哥，已终结的旧短休按历史预算选入；当前邀约、许可房选与周期仍完整保留。
    old_appointments = before["intimacy"]["appointments"]
    new_appointments = sent["intimacy"]["appointments"]
    assert {
        key: value
        for key, value in new_appointments.items()
        if key not in {"arrangements", "room_options"}
    } == {
        key: value
        for key, value in old_appointments.items()
        if key not in {"arrangements", "room_options"}
    }
    current = [
        row
        for row in old_appointments["arrangements"]
        if row["status"]
        not in {"completed", "declined", "cancelled", "expired", "missed", "interrupted", "ended"}
    ]
    assert all(row in new_appointments["arrangements"] for row in current)
    assert all(row in old_appointments["arrangements"] for row in new_appointments["arrangements"])
    historical = [row for row in old_appointments["arrangements"] if row not in current]
    for index, row in enumerate(historical):
        trace = prompt.trace[f"intimacy.appointments.arrangements:{index}"]
        assert trace["source"] == "intimacy.appointments.arrangements"
        assert trace["included"] == (row in new_appointments["arrangements"])
    room_ids = {
        row["room_id"] for row in before["intimacy"]["overnights"] + current if row.get("room_id")
    }
    for room in old_appointments["room_options"]:
        if room["id"] in room_ids:
            assert room in new_appointments["room_options"]
    assert all(
        room in old_appointments["room_options"] for room in new_appointments["room_options"]
    )
    assert sent["intimacy"]["expressed_boundaries"] == before["intimacy"]["expressed_boundaries"]
    assert sent["intimacy"]["own_perspective"] == before["intimacy"]["own_perspective"]
    assert before == context
