"""明确动作必须变成真实工作记录，并进入 NPC 的后续对话。"""

import json
from datetime import timedelta

import pytest

from world_engine.actions import ActionService
from world_engine.conversations import ConversationService
from world_engine.decisions import NpcReply
from world_engine.domain import ActionType
from world_engine.engine import WorldEngine
from world_engine.player_action_flow import react_to_action
from world_engine.player_inputs import parse_player_input
from world_engine.repository import WorldRepository, to_iso

REQUEST = "北境巡图确实有几项要紧事。沿途记下道路与星位，把旧图誊清核对。"


class ObservingProvider:
    name = "observing-test"

    def __init__(self, database):
        self.database = database
        self.contexts = []
        self.fail = False

    def plan_player_action(self, *args):
        raise AssertionError("明确动作及明确台词不应再次交给规划器猜测渠道")

    def respond_to_player(self, *, npc, player, context):
        if self.fail:
            raise RuntimeError("模拟模型不可用")
        if context["channel"] == "action_observation":
            with self.database.read() as connection:
                assert connection.execute(
                    "SELECT 1 FROM world_events WHERE id=?",
                    (context["decision"]["source_event_id"],),
                ).fetchone()
        self.contexts.append(context)
        return NpcReply(
            reply="道路笔记可以先收好，星位和旧图细节还需要继续核验。", social_move="answer"
        )


@pytest.fixture
def scenario(database, settings):
    repo = WorldRepository()
    with database.write() as connection:
        wid = repo.create_world(
            connection, name="行动对话回归", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repo.get_snapshot(connection, wid)
        npc = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=wid,
            name="旅人",
            identity="巡图助手",
            location_id=npc.location_id,
            traits=[],
            goal="记录沿途见闻",
        )
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (player.longitude, player.latitude, npc.id),
        )
        eid = ActionService._record_event(
            connection,
            world_id=wid,
            tick_id="request",
            occurred_at=snapshot.world.current_time,
            event_type="action.socialize",
            actor_id=player.id,
            target_id=npc.id,
            location_id=npc.location_id,
            summary=REQUEST,
            payload={"reply": REQUEST},
        )
        ConversationService().record_exchange(
            connection,
            world_id=wid,
            npc_id=npc.id,
            counterpart_id=player.id,
            event_id=eid,
            world_time=snapshot.world.current_time,
            player_text="巡图需要做些什么？",
            npc_text=REQUEST,
        )
    provider = ObservingProvider(database)
    engine = WorldEngine(database, settings, decision_provider=provider)
    return engine, provider, wid, player.id, npc.id, eid


@pytest.mark.parametrize("prefix", ["动作：", "动作:", "行动 ： "])
def test_explicit_action_survives_locked_dialogue_and_creates_artifacts(database, scenario, prefix):
    engine, provider, wid, player, npc, request_event = scenario
    text = prefix + "记录道路，誊清核对旧图"
    assert not engine.preview_player_intent(wid, text, target_character_id=npc).requires_form
    result = engine.submit_player_intent(wid, text, target_character_id=npc)
    assert result.outcome.action == ActionType.ACTIVITY and result.outcome.accepted
    assert result.npc_reply and result.npc_reply_error is None
    assert len(result.activity_progress) == 3
    context = provider.contexts[-1]
    assert context["channel"] == "action_observation"
    assert context["player_text"] == ""
    assert context["decision"]["original_request"] == REQUEST
    assert "星位观察记录" in context["decision"]["remaining_tasks"]
    with database.read() as connection:
        records = connection.execute(
            "SELECT * FROM player_activity_records WHERE world_id=?", (wid,)
        ).fetchall()
        assert len(records) == 3
        assert all(row["request_event_id"] == request_event for row in records)
        assert (
            connection.execute("SELECT energy FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 94
        )
        turns = connection.execute(
            "SELECT content,message_kind FROM npc_conversation_turns WHERE message_kind='action'"
        ).fetchall()
        assert len(turns) == 1 and "行动结果" in turns[0]["content"]
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM world_events WHERE id=?", (result.outcome.event_id,)
            ).fetchone()[0]
        )
        assert payload["input_kind"] == "action" and "dialogue" not in payload


def test_action_uses_nearby_previous_npc_and_is_recalled_after_one_hour(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    engine.submit_player_intent(wid, "动作：记录道路，誊清核对旧图")
    assert provider.contexts[-1]["npc"]["id"] == npc
    with database.write() as connection:
        snapshot = engine.repository.get_snapshot(connection, wid)
        connection.execute(
            'UPDATE worlds SET "current_time"=? WHERE id=?',
            (to_iso(snapshot.world.current_time + timedelta(hours=2)), wid),
        )
    engine.submit_player_intent(wid, "说话：道路记录接下来怎么核对？", target_character_id=npc)
    context = provider.contexts[-1]
    assert context["player_activity_records"]
    assert any("玩家行动" in line for line in context["recalled_past_conversation"])


def test_missing_model_does_not_undo_or_repeat_action(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    provider.fail = True
    result = engine.submit_player_intent(wid, "动作：记录道路", target_character_id=npc)
    assert result.outcome.accepted and result.npc_reply_error
    provider.fail = False
    first = react_to_action(engine, wid, result.outcome.event_id)
    second = react_to_action(engine, wid, result.outcome.event_id)
    assert first == second
    with database.read() as connection:
        assert (
            connection.execute("SELECT energy FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 98
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM player_activity_records WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM world_events WHERE world_id=? "
                "AND event_type='action.reaction'",
                (wid,),
            ).fetchone()[0]
            == 1
        )


def test_distant_npc_cannot_observe_private_action(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    with database.write() as connection:
        connection.execute("UPDATE characters SET longitude=longitude+10 WHERE id=?", (npc,))
    result = engine.submit_player_intent(wid, "动作：记录道路", target_character_id=npc)
    assert result.outcome.accepted and result.npc_reply is None
    assert provider.contexts == []
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM character_memories WHERE character_id=? AND event_id=?",
                (npc, result.outcome.event_id),
            ).fetchone()
            is None
        )


def test_unknown_action_never_becomes_speech_or_free_money(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    result = engine.submit_player_intent(
        wid, "动作：瞬移到北境并获得一万金币", target_character_id=npc
    )
    assert not result.outcome.accepted
    assert provider.contexts[-1]["decision"]["accepted"] is False
    with database.read() as connection:
        assert (
            connection.execute("SELECT money FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 30
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM player_activity_records WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 0
        )


def test_explicit_speech_with_action_words_stays_dialogue(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    result = engine.submit_player_intent(
        wid, "说话：记录道路后要不要攻击盗贼？", target_character_id=npc
    )
    assert result.outcome.action == ActionType.SOCIALIZE
    assert provider.contexts[-1]["player_text"] == "记录道路后要不要攻击盗贼？"
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM player_activity_records").fetchone()[0] == 0


def test_explicit_rest_uses_existing_rules_not_dialogue(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    with database.write() as connection:
        connection.execute("UPDATE characters SET energy=30 WHERE id=?", (player,))
    result = engine.submit_player_intent(wid, "动作：休息", target_character_id=npc)
    assert result.outcome.action == ActionType.REST
    with database.read() as connection:
        assert (
            connection.execute("SELECT energy FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 70
        )


def test_input_prefix_is_an_explicit_protocol():
    assert parse_player_input(" 动作 ：\n记录道路").kind == "action"
    assert parse_player_input("他说动作：记录道路").kind == "auto"
