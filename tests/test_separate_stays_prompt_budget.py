"""哥哥，真实独立存档的两房、本人选择和会合条款必须一起装进原预算。"""

# ruff: noqa: F811

import copy
import json
from datetime import timedelta

import pytest
from test_appointments import people
from test_journey_room_appointments import current_time, room_at_stop
from test_lived_world import town  # noqa: F401
from test_overnight_stays import stopped_long_journey

from world_engine.engine import WorldEngine
from world_engine.repository import to_iso
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import build_npc_prompt
from world_engine.separate_stays import SeparateStayIntent, SeparateStayService


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("long_history", [False, True])
def test_actual_separate_rooms_keep_consent_and_reunion_within_8192(town, accepted, long_history):
    e = town
    jid = stopped_long_journey(e)
    mine = room_at_stop(e, jid, owner=e.player, name="玩家独立客房")
    yours = room_at_stop(e, jid, owner=e.npc, name="旅伴独立客房")
    hidden = room_at_stop(e, jid, owner=e.player, parent=mine, name="尚不认识的深处内室")
    at = current_time(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        choice = SeparateStayIntent(
            operation="propose",
            journey_id=jid,
            own_room_id=mine,
            other_room_id=yours,
            arrival_world_time=to_iso(at + timedelta(minutes=30)),
            minutes=480,
            reunion_window_minutes=60,
            statement="我们各住自己的独立客房休息八小时，醒后六十分钟会合窗内在原到站室外会合，房租和进屋各自处理。",
        )
        sid = SeparateStayService.apply(c, player, npc, at, choice)["stay_id"]
        if accepted:
            SeparateStayService.apply(
                c,
                npc,
                player,
                at,
                SeparateStayIntent(
                    operation="accept",
                    stay_id=sid,
                    statement="我接受这次分房八小时与醒后会合安排。",
                ),
            )
    engine = WorldEngine(e.db, e.settings)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(
                c,
                snapshot=snapshot,
                npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player),
                player_text="关于刚才的分房八小时休息和醒后会合，你现在有什么想法？",
                conversation=None,
                channel="in_person",
                interaction="核验真实的分房住宿上下文",
            )
        if long_history:
            # 哥哥，压力样本重复真实条款和已有原话，不伪造已经完成的相处事实。
            context["recent_conversation"] *= 30
            context["player_text"] += (
                "我们各自进自己的房间，愿不愿意仍由你决定，休息后在原到站室外会合；同意这次安排不表示同意进入对方房间，也不自动恢复旅行。"
            ) * 3
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context, max_input_tokens=8192)
        sent = json.loads(prompt.messages[1]["content"])["intimacy"]
        assert prompt.input_tokens <= 8192
        assert sent["separate_stays"] == before["intimacy"]["separate_stays"]
        assert sent["journeys"] == before["intimacy"]["journeys"]
        assert {mine, yours}.issubset({r["id"] for r in sent["separate_stay_rooms"]})
        assert hidden not in {r["id"] for r in sent["separate_stay_rooms"]}
        assert "separate_stay_intent" in prompt.messages[0]["content"]
        assert "本轮仍只输出一个JSON对象" in prompt.messages[0]["content"]
        assert before == context
    finally:
        engine.close()


def test_budget_can_drop_terminal_history_without_changing_current_room_allocation():
    current = {
        "id": "stay",
        "status": "accepted",
        "own_room_id": "mine",
        "other_room_id": "yours",
        "reunion_status": "waiting",
    }
    old = {
        "id": "old-visit",
        "status": "completed",
        "statement": "过去已完成的真实约会原话。" * 1000,
    }
    context = {
        "player_text": "我们这次各住各的，醒后会合。",
        "intimacy": {
            "separate_stays": [current],
            "separate_stay_rooms": [
                {"id": "mine", "name": "本人房间"},
                {"id": "yours", "name": "对方房间"},
            ],
            "appointments": {"arrangements": [old]},
            "overnights": [],
            "journeys": [],
            "shared_plans": [],
        },
    }
    before = copy.deepcopy(context)
    prompt = build_npc_prompt("保留本人选择与已知房间。", context, max_input_tokens=700)
    sent = json.loads(prompt.messages[1]["content"])["intimacy"]
    assert sent["separate_stays"] == [current]
    assert {r["id"] for r in sent["separate_stay_rooms"]} == {"mine", "yours"}
    assert sent["appointments"]["arrangements"] == []
    assert not prompt.trace["intimacy.appointments.arrangements:0"]["included"]
    assert context == before
