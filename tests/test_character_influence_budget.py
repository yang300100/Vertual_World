"""哥哥，实际选择要保留有来源的本人观点，空输入也不能清空人物依据。"""

# ruff: noqa: F811
import copy
import json
from uuid import uuid4

import pytest
from test_character_influences import register_lore
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.engine import WorldEngine
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt


@pytest.mark.parametrize("player_text", ["", "你愿意按自己的想法一起散步十分钟吗？"])
@pytest.mark.parametrize("pending", [False, True])
def test_registered_individual_exception_and_local_sources_survive_both_real_budgets(town, player_text, pending):
    e = town
    own = "我知道街区散步习惯，仍偏好先独处，是否同行由我自己决定。"
    public = "街区居民常在散步前问明彼此来意，个人可以作不同选择。"
    local = "广场有人认为十分钟散步正合适，这只是地方说法。"
    with e.db.write() as c:
        event = ActionService._record_event(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="action.socialize", actor_id=e.npc, target_id=e.player,
            location_id=e.loc["id"], summary="双方真实谈到散步与相处习惯。", payload={},
        )
        world = (e.wid, event, e.npc, e.player, e.loc["id"])
        register_lore(c, world, "public_lore", content=public)
        register_lore(c, world, "local_claim", author=e.npc, content=local)
        register_lore(c, world, "character_belief", author=e.npc, content=own)
    if pending:
        response = e.client.post(f"/api/worlds/{e.wid}/companions", json={"recipient_id": e.npc, "minutes": 10})
        assert response.status_code == 200 and response.json()["status"] == "pending"
    engine = WorldEngine(e.db, e.settings, e.model)
    try:
        with e.db.read() as c:
            snapshot = engine.repository.get_snapshot(c, e.wid)
            context = engine.dialogue_context.build(
                c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player), player_text=player_text,
                conversation=None, channel="in_person", interaction="本人自主考虑是否交谈或同行",
                decision_details={"opportunity": "真实散步交流机会", "may_decline": True},
            )
        assert context["player_text"] == player_text
        facts = context["character_influences"]
        assert facts[0]["content"] == own
        assert {fact["content"] for fact in facts} == {own, public, local}
        assert {fact["provenance"]["knowledge_level"] for fact in facts} == {
            "character_belief", "public_lore", "local_claim",
        }
        assert facts[0]["provenance"]["author_character_id"] == e.npc
        assert all(fact["provenance"]["source_event_id"] == event for fact in facts)
        assert not {fact["id"] for fact in facts}.intersection(
            fact.get("id") for fact in context["knowledge_context"]
        )
        context["recent_conversation"] = [
            "[2030-01-01T07:00:00Z] 玩家：" + "以前谈过散步。" * 1200,
            "[2030-01-01T07:00:01Z] NPC：我记得那次交谈。",
        ]
        before = copy.deepcopy(context)
        prompt = build_npc_prompt(npc_reply_system_prompt(), context)
        assert prompt.input_tokens <= 8192
        packed = json.loads(prompt.messages[1]["content"])
        assert packed["character_influences"] == facts
        if pending:
            assert packed["intimacy"]["companion_proposals"][0]["status"] == "pending"
            assert prompt.trace["optional_budget"]["limit"] == max(7400, prompt.trace["mandatory"]["tokens"])
        assert context == before
        with e.db.read() as c:
            assert not c.execute("SELECT 1 FROM companion_sessions WHERE status='active'").fetchone()
    finally:
        engine.close()


def test_required_personal_source_never_truncates_to_make_an_oversized_prompt_pass():
    fact = {"id": "dynamic:actual-view", "content": "本人完整的相处观点。" * 500,
            "provenance": {"knowledge_level": "character_belief", "source_event_id": "source",
                           "registration_id": "registration", "author_character_id": "npc"}}
    context = {"player_text": "", "character_influences": [fact]}
    before = copy.deepcopy(context)
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt("规" * 5000, context, max_input_tokens=8192)
    assert context == before
