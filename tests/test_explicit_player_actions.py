"""明确动作必须变成真实工作记录，并进入 NPC 的后续对话。"""

import json
import time
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from world_engine.actions import ActionService
from world_engine.conversations import ConversationService
from world_engine.decisions import NpcReply
from world_engine.domain import ActionType
from world_engine.engine import WorldEngine
from world_engine.player_action_flow import action_reaction_status, react_to_action
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
            reply="道路笔记可以先收好，星位和旧图细节还需要继续核验。",
            stage_direction="他眯起眼看了看记录。",
            social_move="answer",
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
    assert result.npc_stage_direction == "他眯起眼看了看记录。"
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


def test_action_reaction_reports_billing_failure(database, scenario):
    from world_engine.model_availability import ModelBillingError

    engine, provider, wid, _, npc, _ = scenario

    def billing_failure(*, npc, player, context):
        raise ModelBillingError("DeepSeek 返回 HTTP 402：账户余额或额度不足")

    provider.respond_to_player = billing_failure
    result = engine.submit_player_intent(wid, "记录道路", target_character_id=npc)
    assert result.outcome.accepted
    assert "额度不足" in result.npc_reply_error


def test_deferred_reaction_returns_settled_action_before_model_reply(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    release = Event()
    entered = Event()
    original = provider.respond_to_player

    def slow_reply(*, npc, player, context):
        entered.set()
        assert release.wait(5)
        return original(npc=npc, player=player, context=context)

    provider.respond_to_player = slow_reply
    try:
        started = time.monotonic()
        result = engine.submit_player_intent(
            wid, "记录道路", target_character_id=npc, defer_action_reaction=True
        )
        assert time.monotonic() - started < 1.0
        assert result.outcome.accepted and result.npc_reply_pending
        assert entered.wait(3)
        with database.read() as connection:
            assert connection.execute(
                "SELECT status FROM player_action_reactions WHERE source_event_id=?",
                (result.outcome.event_id,),
            ).fetchone()[0] == "pending"
        with database.write() as connection:
            connection.execute("UPDATE worlds SET version=version+1 WHERE id=?", (wid,))
    finally:
        release.set()

    for _ in range(100):
        with database.read() as connection:
            status = connection.execute(
                "SELECT status FROM player_action_reactions WHERE source_event_id=?",
                (result.outcome.event_id,),
            ).fetchone()[0]
        if status == "ready":
            break
        time.sleep(0.05)
    assert status == "ready"
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM player_activity_records WHERE world_id=?", (wid,)
        ).fetchone()[0] == 1


def test_pending_reaction_resumes_after_status_query(database, scenario):
    engine, provider, wid, _, npc, event_id = scenario
    with database.write() as connection:
        connection.execute(
            "INSERT INTO player_action_reactions(source_event_id,world_id,npc_id,status) "
            "VALUES (?,?,?,'pending')",
            (event_id, wid, npc),
        )
    assert action_reaction_status(engine, wid, event_id)["status"] == "pending"
    for _ in range(100):
        result = action_reaction_status(engine, wid, event_id)
        if result["status"] != "pending":
            break
        time.sleep(0.05)
    assert result["status"] == "ready"
    assert result["reply"]


def test_action_reaction_can_give_owned_item_to_present_player(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    item_type_id, item_id = str(uuid4()), str(uuid4())
    with database.write() as connection:
        connection.execute(
            "INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,?,?,?)",
            (item_type_id, "铜钥匙", "tool", 10),
        )
        connection.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,"
            "quantity,condition,owner_character_id) VALUES (?,?,?,?,'character_inventory',1,100,?)",
            (item_id, wid, item_type_id, npc, npc),
        )
        connection.execute("UPDATE characters SET inventory_capacity=20 WHERE id=?", (player,))

    def gift_reply(*, npc, player, context):
        return NpcReply.model_validate({
            "reply": "这把钥匙给你。",
            "stage_direction": "他递到你手里。随后笑了笑。",
            "action_intent": {"kind": "give_item", "target_id": item_id},
            "fallback_reply": "现在还拿不出来。",
        })

    provider.respond_to_player = gift_reply
    result = engine.submit_player_intent(wid, "动作：记录道路", target_character_id=npc)
    assert result.outcome.accepted
    assert result.npc_reply == "这把钥匙给你。"
    assert result.npc_stage_direction and "交给" in result.npc_stage_direction
    assert "递到" not in result.npc_stage_direction
    with database.read() as connection:
        gifted = connection.execute(
            "SELECT quantity FROM item_instances WHERE container_id=? AND item_type_id=?",
            (player, item_type_id),
        ).fetchone()
        assert gifted and gifted[0] == 1
        reaction = connection.execute(
            "SELECT payload_json FROM world_events WHERE event_type='action.reaction' "
            "AND world_id=? ORDER BY created_at DESC LIMIT 1", (wid,),
        ).fetchone()
        payload = json.loads(reaction[0])
        assert payload["npc_action_status"] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_changes WHERE world_id=? AND source_event_id=?",
            (wid, payload["npc_action_event_id"]),
        ).fetchone()[0] > 0


@pytest.mark.parametrize("phrase", [
    "将铜钥匙赠与{name}", "把铜钥匙送给{name}",
    "动作：将铜钥匙赠与{name}", "我想把铜钥匙送给{name}",
])
def test_unprefixed_gift_moves_owned_item(database, scenario, phrase):
    engine, _, wid, player_id, npc_id, _ = scenario
    item_type_id, item_id = str(uuid4()), str(uuid4())
    with database.write() as connection:
        npc_name = connection.execute("SELECT name FROM characters WHERE id=?", (npc_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,?,?,?)",
            (item_type_id, "铜钥匙", "tool", 10),
        )
        connection.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,"
            "quantity,condition,owner_character_id) VALUES (?,?,?,?,'character_inventory',1,100,?)",
            (item_id, wid, item_type_id, player_id, player_id),
        )
    result = engine.submit_player_intent(
        wid, phrase.format(name=npc_name), defer_action_reaction=True,
    )
    assert result.outcome.accepted
    with database.read() as connection:
        item = connection.execute(
            "SELECT container_id,owner_character_id FROM item_instances WHERE id=?", (item_id,),
        ).fetchone()
        audit = connection.execute(
            "SELECT COUNT(*) FROM inventory_changes WHERE source_event_id=?",
            (result.outcome.event_id,),
        ).fetchone()[0]
    assert item["container_id"] == npc_id
    assert item["owner_character_id"] == npc_id
    assert audit > 0
    assert result.outcome.action is ActionType.TRANSFER


def test_gift_rejects_missing_item_without_world_change(database, scenario):
    engine, _, wid, _, npc_id, _ = scenario
    with database.read() as connection:
        npc_name = connection.execute("SELECT name FROM characters WHERE id=?", (npc_id,)).fetchone()[0]
        before = connection.execute("SELECT version FROM worlds WHERE id=?", (wid,)).fetchone()[0]
    with pytest.raises(ValueError, match="背包中没有"):
        engine.submit_player_intent(wid, f"将不存在的钥匙赠与{npc_name}")
    with database.read() as connection:
        after = connection.execute("SELECT version FROM worlds WHERE id=?", (wid,)).fetchone()[0]
        transfers = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND event_type='action.transfer'",
            (wid,),
        ).fetchone()[0]
    assert before == after
    assert transfers == 0


def test_intent_preview_exposes_checked_gift_details(database, scenario):
    engine, _, wid, _, npc_id, _ = scenario
    with database.read() as connection:
        npc_name = connection.execute(
            "SELECT name FROM characters WHERE id=?", (npc_id,),
        ).fetchone()[0]
    preview = engine.preview_player_intent(wid, f"将铜钥匙赠与{npc_name}")
    assert preview.route_kind == "action"
    assert preview.action_kind == "gift"
    assert preview.route_detail == f"将铜钥匙赠与{npc_name}"
    assert preview.item_name == "铜钥匙"
    assert preview.target_label == npc_name
    assert not preview.requires_form


def test_intent_preview_exposes_checked_speech_details(database, scenario):
    engine, _, wid, _, npc_id, _ = scenario
    preview = engine.preview_player_intent(
        wid, "说话：你好", target_character_id=npc_id,
    )
    assert preview.route_kind == "speech"
    assert preview.route_detail == "你好"
    assert preview.target_character_id == npc_id
    assert not preview.requires_form


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
    assert provider.contexts == []
    assert result.npc_reply is None and result.npc_reply_error is None
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


def test_explicit_rest_starts_persistent_activity_not_instant_recovery(database, scenario):
    engine, provider, wid, player, npc, _ = scenario
    with database.write() as connection:
        connection.execute("UPDATE characters SET energy=30 WHERE id=?", (player,))
    result = engine.submit_player_intent(wid, "动作：休息", target_character_id=npc)
    assert result.outcome.action == ActionType.REST
    with database.read() as connection:
        assert (
            connection.execute("SELECT energy FROM characters WHERE id=?", (player,)).fetchone()[0]
            == 30
        )
        activity = connection.execute(
            "SELECT * FROM character_life_activities WHERE character_id=? AND status='running'",
            (player,),
        ).fetchone()
        assert activity is not None and activity["kind"] == "rest"
        assert activity["source_event_id"] == result.outcome.event_id


def test_input_prefix_is_an_explicit_protocol():
    assert parse_player_input(" 动作 ：\n记录道路").kind == "action"
    assert parse_player_input("他说动作：记录道路").kind == "auto"
