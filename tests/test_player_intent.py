from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.conversations import ConversationService
from world_engine.decisions import DecisionProviderError, NpcReply
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent, IntentParseResult
from world_engine.repository import WorldRepository, to_iso
from world_engine.seeder import create_iserra_world


class _ConversationCapturingProvider:
    """模拟第二轮模型偏离话题，用于验证引擎会恢复连续对话。"""

    name = "conversation-capture"

    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        self.intents: list[str] = []
        self.reply_contexts: list[dict[str, object]] = []

    def propose(self, snapshot, characters):  # type: ignore[no-untyped-def]
        return []

    def plan_player_action(self, snapshot, player, intent):  # type: ignore[no-untyped-def]
        self.intents.append(intent)
        if len(self.intents) == 1:
            return ActionProposal(
                actor_id=player.id,
                action=ActionType.SOCIALIZE,
                target_id=self.target_id,
                reason="我想询问对方的工作。",
                dialogue="我想了解你的工作。",
                reply="你问这个是想入行吗？",
            )
        return ActionProposal(
            actor_id=player.id,
            action=ActionType.IDLE,
            reason="错误地忽略了追问。",
        )

    def respond_to_player(self, *, npc, player, context):  # type: ignore[no-untyped-def]
        self.reply_contexts.append(context)
        return NpcReply(reply="这是测试模型生成的回应。", social_move="answer")


class _IndependentNpcReplyProvider(_ConversationCapturingProvider):
    """验证目标 NPC 使用自己的对话上下文回应。"""

    def __init__(self, target_id: str) -> None:
        super().__init__(target_id)
        self.reply_context: dict[str, object] | None = None

    def respond_to_player(self, *, npc, player, context):  # type: ignore[no-untyped-def]
        self.reply_context = context
        return NpcReply(reply="这件事我暂时不想在街上细说。", social_move="boundary")


class _FailingConversationProvider:
    """模拟玩家行动模型不可用，验证对话不会退回职业通用台词。"""

    name = "deepseek"

    def propose(self, snapshot, characters):  # type: ignore[no-untyped-def]
        raise RuntimeError("模拟模型故障")

    def plan_player_action(self, snapshot, player, intent):  # type: ignore[no-untyped-def]
        raise RuntimeError("模拟模型故障")


class _FakeNpcModelBackend:
    """为 API 审阅测试提供结构化模型回应。"""

    def complete(self, *, schema, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(data=schema.validate_python({"reply": "这是测试模型生成的回应。"}))


def test_submit_intent_without_player_raises(database, settings) -> None:
    """没有玩家角色时，提交意图应报错。"""
    world_id = create_iserra_world(database)
    engine = WorldEngine(database, settings)
    with pytest.raises(ValueError):
        engine.submit_player_intent(world_id, "去东澜港看看")


def test_submit_intent_travel_settles(database, settings) -> None:
    """玩家意图“去东澜港”应转成一次 TRAVEL 行动并落库。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        start_location = next(
            location.id for location in snapshot.locations if location.name == "澜誓城"
        )
    with database.write() as connection:
        repo.create_player_character(
            connection,
            world_id=world_id,
            name="旅人",
            identity="远行学者",
            location_id=start_location,
            traits=["谨慎"],
            goal="记录伊瑟拉",
        )

    engine = WorldEngine(database, settings)
    result = engine.submit_player_intent(world_id, "我要去东澜港看看")

    assert result.outcome.accepted is True
    assert result.outcome.action is ActionType.TRAVEL
    assert result.provider == "rules"
    assert result.current_version == result.previous_version + 1

    with database.read() as connection:
        events = repo.list_events(connection, world_id, limit=20)
    assert any(event["event_type"] == "action.travel" for event in events)


def test_follow_up_dialogue_keeps_target_and_recent_turns(database, settings) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        initial = repo.get_snapshot(connection, world_id)
        target = initial.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="学习本地手艺",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ? WHERE id = ?",
            (player.longitude, player.latitude, target.id),
        )
    provider = _ConversationCapturingProvider(target.id)
    engine = WorldEngine(database, settings, decision_provider=provider)

    first = engine.submit_player_intent(world_id, "我想了解你的工作。")
    second_text = "是的，干这一行需要专业储备吗？"
    second = engine.submit_player_intent(world_id, second_text)

    assert first.outcome.action is ActionType.SOCIALIZE
    assert second.outcome.action is ActionType.SOCIALIZE
    assert len(provider.intents) == 1
    assert len(provider.reply_contexts) == 2
    assert "这是测试模型生成的回应。" in "\n".join(
        provider.reply_contexts[1]["recent_conversation"]  # type: ignore[arg-type]
    )
    assert provider.reply_contexts[1]["player_text"] == second_text
    assert any(
        "我想了解你的工作" in memory["summary"]
        for memory in provider.reply_contexts[1]["recent_private_memories"]  # type: ignore[union-attr]
    )
    with database.read() as connection:
        event = next(
            item
            for item in repo.list_events(connection, world_id, limit=20)
            if item["id"] == second.outcome.event_id
        )
    assert event["target_id"] == target.id
    assert event["payload"]["dialogue"] == second_text
    assert event["payload"]["reply"] == "这是测试模型生成的回应。"
    with database.read() as connection:
        turns = connection.execute(
            "SELECT COUNT(*) FROM npc_conversation_turns WHERE event_id IN (?, ?)",
            (first.outcome.event_id, second.outcome.event_id),
        ).fetchone()[0]
        npc_memories = connection.execute(
            """
            SELECT COUNT(*) FROM character_memories
            WHERE character_id = ? AND event_id IN (?, ?) AND memory_type = 'experienced'
            """,
            (target.id, first.outcome.event_id, second.outcome.event_id),
        ).fetchone()[0]
    assert turns == 4
    assert npc_memories == 2

    with database.write() as connection:
        world_time = repo.get_snapshot(connection, world_id).world.current_time
        connection.execute(
            "UPDATE worlds SET current_time = ? WHERE id = ?",
            (to_iso(world_time + timedelta(hours=1, seconds=1)), world_id),
        )
    with database.read() as connection:
        expired_snapshot = repo.get_snapshot(connection, world_id)
        expired_target = expired_snapshot.character_by_id(target.id)
        context = ConversationService().load_context(
            connection,
            snapshot=expired_snapshot,
            player_id=next(item.id for item in expired_snapshot.characters if item.is_player),
            npc=expired_target,
        )
    assert context is None


def test_target_npc_generates_its_own_reply_from_private_context(
    database, settings, tmp_path
) -> None:
    (tmp_path / "common.md").write_text(
        """<!-- rag: audience=character_common; always_include=true; tags=对话测试 -->
# 居民常识

市集中的货物都有来源和占有人，不能因为无人看守就随意取走。
""",
        encoding="utf-8",
    )
    settings = replace(settings, knowledge_paths=(tmp_path,), knowledge_enabled=True)
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        target = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="打听消息",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ? WHERE id = ?",
            (player.longitude, player.latitude, target.id),
        )
    provider = _IndependentNpcReplyProvider(target.id)
    result = WorldEngine(database, settings, decision_provider=provider).submit_player_intent(
        world_id, "与对方谈谈粮食的事", target_character_id=target.id
    )

    assert result.outcome.accepted is True
    assert provider.reply_context is not None
    assert provider.reply_context["npc"]["id"] == target.id  # type: ignore[index]
    assert "npc_card" in provider.reply_context
    assert provider.reply_context["scene"]["location"]["id"] == target.location_id  # type: ignore[index]
    assert any(
        "货物都有来源" in item["content"]
        for item in provider.reply_context["knowledge_context"]  # type: ignore[union-attr]
    )
    assert (
        provider.reply_context["budget_trace"]["total"]
        <= provider.reply_context["budget_trace"]["limit"]
    )  # type: ignore[index]
    assert "这件事我暂时不想在街上细说。" in result.outcome.summary
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM npc_character_cards WHERE character_id = ?", (target.id,)
        ).fetchone()[0] == 1


def test_long_conversation_creates_traceable_episode_and_recalls_it(
    database, settings
) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        target = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="追查粮车",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ? WHERE id = ?",
            (player.longitude, player.latitude, target.id),
        )
    provider = _ConversationCapturingProvider(target.id)
    engine = WorldEngine(database, settings, decision_provider=provider)
    for text in (
        "我想问北门失踪粮车的事。",
        "你最后一次看见粮车是什么时候？",
        "我们约定明早继续追查，请记得。",
    ):
        engine.submit_player_intent(world_id, text, target_character_id=target.id)

    with database.read() as connection:
        episode = connection.execute(
            "SELECT * FROM npc_conversation_episodes WHERE world_id = ?",
            (world_id,),
        ).fetchone()
    assert episode is not None
    assert (episode["first_turn_index"], episode["last_turn_index"]) == (1, 6)
    assert "粮车" in episode["summary"]
    assert len(episode["source_hash"]) == 64
    assert "明早" in episode["open_commitments_json"]

    with database.write() as connection:
        current_time = repo.get_snapshot(connection, world_id).world.current_time
        connection.execute(
            "UPDATE worlds SET current_time = ? WHERE id = ?",
            (to_iso(current_time + timedelta(hours=2)), world_id),
        )
    engine.submit_player_intent(
        world_id,
        "关于北门粮车，我们接着说。",
        target_character_id=target.id,
    )
    recalled = provider.reply_contexts[-1]["conversation_episodes"]
    assert recalled
    assert "粮车" in recalled[0]["summary"]


def test_group_dialogue_selects_requested_speakers_and_records_each_reply(
    database, settings
) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        targets = snapshot.characters[:2]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=targets[0].location_id,
            traits=[],
            goal="听取众人意见",
        )
        for target in targets:
            connection.execute(
                """
                UPDATE characters
                SET longitude = ?, latitude = ?, current_location_id = ?
                WHERE id = ?
                """,
                (player.longitude, player.latitude, player.location_id, target.id),
            )
    provider = _ConversationCapturingProvider(targets[0].id)
    result = WorldEngine(
        database, settings, decision_provider=provider
    ).submit_group_dialogue(
        world_id,
        "你们觉得北门粮车应该先从哪里查起？",
        participant_ids=[target.id for target in targets],
        max_speakers=2,
    )

    assert len(result["replies"]) == 2
    assert {item["character_id"] for item in result["replies"]} == {
        target.id for target in targets
    }
    assert len(provider.reply_contexts) == 2
    assert provider.reply_contexts[0]["channel"] == "group_scene"
    assert provider.reply_contexts[0]["decision"]["prior_group_replies"] == []
    assert len(provider.reply_contexts[1]["decision"]["prior_group_replies"]) == 1
    with database.read() as connection:
        events = connection.execute(
            """
            SELECT COUNT(*) FROM world_events
            WHERE tick_id = ? AND event_type = 'action.socialize'
            """,
            (f"group-dialogue:{result['group_dialogue_id']}",),
        ).fetchone()[0]
        npc_memories = connection.execute(
            """
            SELECT COUNT(*) FROM character_memories
            WHERE character_id IN (?, ?) AND memory_type = 'experienced'
            """,
            tuple(target.id for target in targets),
        ).fetchone()[0]
    assert events == 2
    assert npc_memories >= 2


def test_model_failure_does_not_write_a_preset_npc_reply(
    database, settings
) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        target = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="打听消息",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ?, energy = 0 WHERE id = ?",
            (player.longitude, player.latitude, target.id),
        )

    engine = WorldEngine(database, settings, decision_provider=_FailingConversationProvider())
    with pytest.raises(DecisionProviderError, match="不支持 NPC 模型对话"):
        engine.submit_player_intent(world_id, f"与{target.name}交谈，最近怎么样？")
    with database.read() as connection:
        assert not [
            event
            for event in repo.list_events(connection, world_id, limit=20)
            if event["event_type"] == "action.socialize"
        ]


def test_player_cannot_start_dialogue_with_npc_outside_one_hundred_meters(database, settings) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        initial = repo.get_snapshot(connection, world_id)
        target = initial.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="探索世界",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ? WHERE id = ?",
                (player.longitude + 0.0015, player.latitude, target.id),
        )

    with pytest.raises(ValueError, match="超过100米"):
        WorldEngine(database, settings).submit_player_intent(
            world_id,
            f"与{target.name}交谈",
            target_character_id=target.id,
        )


def test_intent_preview_returns_purchase_form_without_writing_world(database, settings) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        target = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="采购药材",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ? WHERE id = ?",
            (player.longitude, player.latitude, target.id),
        )
    class ParserBackend:
        def complete(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                data=IntentParseResult(
                    operation="purchase",
                    target_character_id=target.id,
                    item_name="疗伤药",
                    amount=3,
                )
            )

    engine = WorldEngine(database, settings)
    engine.intent_parser = IntentParserAgent(ParserBackend())  # type: ignore[arg-type]
    preview = engine.preview_player_intent(
        world_id,
        f"我想从{target.name}那里买点能疗伤的药，三枚铜币可以吗？",
        target_character_id=target.id,
    )

    assert preview.requires_form is True
    assert preview.operation == "purchase"
    assert preview.target_character_id == target.id
    assert preview.item_name == "疗伤药"
    assert preview.amount == 3
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM action_effects").fetchone()[0] == 0


def test_learning_condition_question_stays_a_normal_dialogue(database, settings) -> None:
    """询问 NPC 愿不愿意教学不等于玩家已经发起学习。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        target = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="探索世界",
        )
        connection.execute(
            "UPDATE characters SET longitude = ?, latitude = ? WHERE id = ?",
            (player.longitude, player.latitude, target.id),
        )

    class MisclassifyingParserBackend:
        def complete(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                data=IntentParseResult(
                    operation="learn",
                    target_character_id=target.id,
                    skill_name="寻星知识",
                )
            )

    engine = WorldEngine(database, settings)
    engine.intent_parser = IntentParserAgent(MisclassifyingParserBackend())  # type: ignore[arg-type]
    preview = engine.preview_player_intent(
        world_id,
        f"与{target.name}交谈：怎么样你才愿意教给我寻星知识呢？",
        target_character_id=target.id,
    )

    assert preview.requires_form is False
    assert preview.operation == "none"


def test_dialogue_purchase_uses_audited_effects_without_immediate_learning(database, settings) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        initial = repo.get_snapshot(connection, world_id)
        target = initial.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=target.location_id,
            traits=[],
            goal="学习草药知识",
        )
        connection.execute(
            """
            UPDATE characters SET longitude = ?, latitude = ?, identity = ?, skills_json = ?
            WHERE id = ?
            """,
            (player.longitude, player.latitude, "药店主", '["草药辨识"]', target.id),
        )
        # 从药店主的真实库存交易，背包中的同类药剂允许堆叠。
        connection.execute(
            """INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type)
               VALUES ('dialogue-stock',?,'healing_potion',?,'character_inventory')""",
            (world_id, target.id),
        )
    provider = _ConversationCapturingProvider(target.id)
    result = WorldEngine(database, settings, decision_provider=provider).submit_player_intent(
        world_id,
        f"向{target.name}支付3铜币购买疗伤药，并询问草药辨识的课程",
        target_character_id=target.id,
    )

    assert result.outcome.accepted is True
    assert "支付3铜币，获得疗伤药" in result.outcome.summary
    with database.read() as connection:
        player_row = connection.execute(
            "SELECT money, skills_json FROM characters WHERE id = ?", (player.id,)
        ).fetchone()
        items = connection.execute(
            """
            SELECT SUM(i.quantity) FROM item_instances i JOIN item_types t ON t.id = i.item_type_id
            WHERE i.container_id = ? AND i.container_type = 'character_inventory' AND t.name = '疗伤药'
            """,
            (player.id,),
        ).fetchone()[0]
        effects = connection.execute(
            "SELECT COUNT(*) FROM action_effects WHERE source_event_id = ? AND status = 'applied'",
            (result.outcome.event_id,),
        ).fetchone()[0]
    assert player_row["money"] == 27
    assert "草药辨识" not in player_row["skills_json"]
    assert items == 3
    assert effects == 1


def test_learning_requires_npc_review_before_skill_is_granted(database, settings, monkeypatch) -> None:
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.write() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        teacher = snapshot.characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="阿澈",
            identity="旅人",
            location_id=teacher.location_id,
            traits=[],
            goal="学习草药知识",
        )
        connection.execute(
            "UPDATE characters SET identity = ?, skills_json = ? WHERE id = ?",
            ("药师", '["草药辨识"]', teacher.id),
        )
    monkeypatch.setattr(
        "world_engine.engine.build_agent_model_backend",
        lambda settings: _FakeNpcModelBackend(),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        review = client.post(
            f"/api/worlds/{world_id}/long-term-requests",
            json={
                "recipient_id": teacher.id,
                "operation_type": "学习",
                "terms": {"skill": "草药辨识", "payment": 3},
            },
        )
        assert review.status_code == 200
        assert review.json()["status"] in {"npc_accepted", "npc_countered"}
        with database.read() as connection:
            before = connection.execute(
                "SELECT skills_json FROM characters WHERE id = ?", (player.id,)
            ).fetchone()["skills_json"]
        assert "草药辨识" not in before
        confirmed = client.post(
            f"/api/worlds/{world_id}/long-term-requests/{review.json()['id']}/confirm",
            json={"accept_counter_terms": True},
        )
        assert confirmed.status_code == 200
    with database.read() as connection:
        after = connection.execute(
            "SELECT skills_json FROM characters WHERE id = ?", (player.id,)
        ).fetchone()["skills_json"]
    assert "草药辨识" in after


def test_api_player_act_endpoint(settings) -> None:
    """POST /player/act 应返回一次成功结算的行动。"""
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "玩家世界", "seed_demo": True}
        ).json()
        world_id = created["world"]["id"]
        start_location = next(
            loc["id"] for loc in created["locations"] if loc["name"] != "河畔住宅"
        )

        created_player = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "阿澈",
                "identity": "旅人",
                "location_id": start_location,
                "traits": [],
                "goal": "记录世界",
            },
        )
        assert created_player.status_code == 201

        act = client.post(
            f"/api/worlds/{world_id}/player/act",
            json={"intent": "我要去河畔住宅看看"},
        )
        assert act.status_code == 201
        body = act.json()
        assert body["outcome"]["accepted"] is True
        assert body["outcome"]["action"] == "travel"
