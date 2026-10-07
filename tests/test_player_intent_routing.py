"""玩家说话与实际行动的边界回归。"""

from __future__ import annotations

import pytest

from world_engine.conversations import ConversationService
from world_engine.decisions import NpcReply, RuleDecisionProvider
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.player_inputs import unprefixed_action_text
from world_engine.repository import WorldRepository


class MisclassifyingProvider:
    name = "misclassifying-test"

    def __init__(self) -> None:
        self.plan_calls = 0

    def plan_player_action(self, snapshot, player, intent):
        self.plan_calls += 1
        return ActionProposal(
            actor_id=player.id,
            action=ActionType.EAT,
            reason="错误地把玩家说话当成进食",
        )

    def respond_to_player(self, *, npc, player, context):
        return NpcReply(reply="我还好，谢谢你关心。")


@pytest.fixture
def dialogue_scene(database, settings):
    repo = WorldRepository()
    with database.write() as connection:
        world_id = repo.create_world(
            connection, name="意图路由回归", minutes_per_tick=60, seed_demo=True
        )
        npc = repo.get_snapshot(connection, world_id).characters[0]
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="玩家",
            identity="旅人",
            location_id=npc.location_id,
            traits=[],
            goal="交谈",
        )
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (player.longitude, player.latitude, npc.id),
        )
        connection.execute("UPDATE characters SET satiety=0 WHERE id=?", (player.id,))
    return world_id, player.id, npc.id


@pytest.mark.parametrize("intent", [
    "动作：走到陌生人2附近", "走到陌生人2附近",
    "动作：移动到陌生人2附近", "移动到陌生人2附近",
    "移动到陌生人2的附近",
])
def test_approach_visible_stranger_changes_position(database, settings, dialogue_scene, intent) -> None:
    world_id, player_id, npc_id = dialogue_scene
    with database.write() as connection:
        player = connection.execute("SELECT latitude,longitude FROM characters WHERE id=?", (player_id,)).fetchone()
        others = connection.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? ORDER BY rowid",
            (world_id, npc_id),
        ).fetchall()
        second_id = others[0][0]
        connection.execute("UPDATE characters SET latitude=? WHERE id=?", (player["latitude"] + 0.0001, npc_id))
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_room_id=NULL WHERE id=?",
            (player["longitude"], player["latitude"] + 0.0002, second_id),
        )
    engine = WorldEngine(database, settings, decision_provider=MisclassifyingProvider())
    preview = engine.preview_player_intent(world_id, intent)
    assert not preview.requires_form
    result = engine.submit_player_intent(world_id, intent)
    assert result.outcome.accepted
    assert result.outcome.action is ActionType.TRAVEL
    assert "陌生人2" in result.outcome.summary
    with database.read() as connection:
        moved = connection.execute("SELECT latitude FROM characters WHERE id=?", (player_id,)).fetchone()
        npc = connection.execute("SELECT latitude FROM characters WHERE id=?", (second_id,)).fetchone()
    assert moved["latitude"] == npc["latitude"]


def test_rejected_action_has_no_npc_reaction(database, settings, dialogue_scene) -> None:
    world_id, player_id, npc_id = dialogue_scene
    result = WorldEngine(database, settings, decision_provider=MisclassifyingProvider()).submit_player_intent(
        world_id, "动作：做一件没有规则的事", target_character_id=npc_id
    )
    assert not result.outcome.accepted
    assert result.npc_reply is None and result.npc_reply_error is None
    with database.read() as connection:
        pending = connection.execute(
            "SELECT COUNT(*) FROM player_action_reactions WHERE source_event_id=?",
            (result.outcome.event_id,),
        ).fetchone()[0]
    assert pending == 0


@pytest.mark.parametrize("reply_template", [
    "{name}，这里的书记。找我有什么事？",
    "我叫{name}，这里的书记。",
    "你没动，也没答我。{name}，商会书记。找我有事？",
    "（她放下笔）{name}。叫这个名字就好。",
    "刚才不是说了吗——{name}。",
])
def test_npc_spoken_name_becomes_known(database, settings, dialogue_scene, reply_template) -> None:
    world_id, player_id, npc_id = dialogue_scene
    with database.read() as connection:
        npc_name = connection.execute("SELECT name FROM characters WHERE id=?", (npc_id,)).fetchone()[0]

    class IntroducingProvider(MisclassifyingProvider):
        def respond_to_player(self, *, npc, player, context):
            return NpcReply(reply=reply_template.format(name=npc_name))

    engine = WorldEngine(database, settings, decision_provider=IntroducingProvider())
    result = engine.submit_player_intent(world_id, "说话：你叫什么名字？", target_character_id=npc_id)
    assert result.outcome.accepted
    with database.read() as connection:
        known = connection.execute(
            "SELECT known_name FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (player_id, npc_id),
        ).fetchone()
    assert known is not None and known["known_name"] == npc_name


def test_saved_self_introduction_is_recovered_for_existing_save(database, settings, dialogue_scene) -> None:
    world_id, player_id, npc_id = dialogue_scene
    with database.read() as connection:
        npc_name = connection.execute("SELECT name FROM characters WHERE id=?", (npc_id,)).fetchone()[0]

    class IntroducingProvider(MisclassifyingProvider):
        def respond_to_player(self, *, npc, player, context):
            return NpcReply(reply=f"{npc_name}，商会的书记。找我有事？")

    WorldEngine(database, settings, decision_provider=IntroducingProvider()).submit_player_intent(
        world_id, "说话：怎么称呼你？", target_character_id=npc_id
    )
    with database.write() as connection:
        connection.execute(
            "DELETE FROM knowledge_evidence WHERE edge_id IN "
            "(SELECT id FROM observer_knowledge_edges WHERE observer_id=? AND subject_id=? AND predicate='name')",
            (player_id, npc_id),
        )
        connection.execute(
            "DELETE FROM observer_knowledge_edges WHERE observer_id=? AND subject_id=? AND predicate='name'",
            (player_id, npc_id),
        )
        connection.execute(
            "UPDATE character_acquaintances SET known_name=NULL WHERE observer_id=? AND subject_id=?",
            (player_id, npc_id),
        )
    database.initialize()
    with database.read() as connection:
        known = connection.execute(
            "SELECT known_name FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (player_id, npc_id),
        ).fetchone()[0]
    assert known == npc_name


def test_npc_chosen_alias_is_visible_without_changing_true_name(database, settings, dialogue_scene) -> None:
    from world_engine.society import SocietyService

    world_id, player_id, npc_id = dialogue_scene

    class AliasProvider(MisclassifyingProvider):
        def respond_to_player(self, *, npc, player, context):
            return NpcReply(reply="叫我阿月。")

    WorldEngine(database, settings, decision_provider=AliasProvider()).submit_player_intent(
        world_id, "说话：怎么称呼你？", target_character_id=npc_id
    )
    with database.read() as connection:
        true_name = connection.execute("SELECT name FROM characters WHERE id=?", (npc_id,)).fetchone()[0]
        assert SocietyService.label(connection, player_id, npc_id) == "阿月"
        assert true_name != "阿月"


def test_npc_role_is_not_mistaken_for_name(database, settings, dialogue_scene) -> None:
    world_id, player_id, npc_id = dialogue_scene

    class RoleOnlyProvider(MisclassifyingProvider):
        def respond_to_player(self, *, npc, player, context):
            return NpcReply(reply="我是商会书记。")

    WorldEngine(database, settings, decision_provider=RoleOnlyProvider()).submit_player_intent(
        world_id, "说话：你叫什么名字？", target_character_id=npc_id
    )
    with database.read() as connection:
        known = connection.execute(
            "SELECT known_name FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
            (player_id, npc_id),
        ).fetchone()
    assert known is not None and known["known_name"] is None


@pytest.mark.parametrize(
    "phrase",
    [
        "你吃了吗？",
        "你用过疗伤药吗？",
        "你去过东澜港吗？",
        "你移动到那个人附近了吗？",
        "你的工作忙吗？",
        "我想向你打听最近的事。",
        "我打算和你谈谈。",
    ],
)
def test_addressed_speech_does_not_execute_an_action(
    database, settings, dialogue_scene, phrase
) -> None:
    world_id, _player_id, npc_id = dialogue_scene
    provider = MisclassifyingProvider()
    result = WorldEngine(database, settings, decision_provider=provider).submit_player_intent(
        world_id, phrase, target_character_id=npc_id
    )

    assert result.outcome.accepted
    assert result.outcome.action is ActionType.SOCIALIZE
    assert result.player_dialogue == phrase
    assert result.npc_reply == "我还好，谢谢你关心。"
    assert provider.plan_calls == 0


def test_unaddressed_greeting_rejects_model_invented_eating(
    database, settings, dialogue_scene
) -> None:
    world_id, player_id, _npc_id = dialogue_scene
    provider = MisclassifyingProvider()
    engine = WorldEngine(database, settings, decision_provider=provider)

    with pytest.raises(ValueError, match="选择要说话的 NPC"):
        engine.submit_player_intent(world_id, "你好呀")

    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0] == 0


def test_named_npc_can_receive_a_food_question_without_target_id(
    database, settings, dialogue_scene
) -> None:
    world_id, _player_id, npc_id = dialogue_scene
    with database.read() as connection:
        npc = WorldRepository().get_snapshot(connection, world_id).character_by_id(npc_id)
    provider = MisclassifyingProvider()

    result = WorldEngine(database, settings, decision_provider=provider).submit_player_intent(
        world_id, f"{npc.name}，你吃了吗？"
    )

    assert result.outcome.action is ActionType.SOCIALIZE
    assert provider.plan_calls == 0


def test_follow_up_food_question_uses_active_conversation(
    database, settings, dialogue_scene
) -> None:
    world_id, _player_id, npc_id = dialogue_scene
    provider = MisclassifyingProvider()
    engine = WorldEngine(database, settings, decision_provider=provider)
    engine.submit_player_intent(world_id, "你好", target_character_id=npc_id)

    result = engine.submit_player_intent(world_id, "你吃了吗？")

    assert result.outcome.action is ActionType.SOCIALIZE
    assert result.player_dialogue == "你吃了吗？"
    assert provider.plan_calls == 0


def test_unknown_player_text_does_not_trigger_autonomous_hunger(
    database, dialogue_scene
) -> None:
    world_id, player_id, _npc_id = dialogue_scene
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    player = snapshot.character_by_id(player_id)

    proposal = RuleDecisionProvider().plan_player_action(snapshot, player, "我在这里发呆")

    assert proposal.action is ActionType.IDLE


def test_fallback_does_not_treat_asking_as_an_attack(
    database, dialogue_scene
) -> None:
    world_id, player_id, _npc_id = dialogue_scene
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    player = snapshot.character_by_id(player_id)

    proposal = RuleDecisionProvider().plan_player_action(snapshot, player, "我想打听消息")

    assert proposal.action is not ActionType.ATTACK


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("打工", ActionType.WORK),
        ("打水", ActionType.IDLE),
        ("打字", ActionType.IDLE),
    ],
)
def test_fallback_does_not_attack_for_nonviolent_da_phrases(
    database, dialogue_scene, phrase, expected
) -> None:
    world_id, player_id, _npc_id = dialogue_scene
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    player = snapshot.character_by_id(player_id)

    proposal = RuleDecisionProvider().plan_player_action(snapshot, player, phrase)

    assert proposal.action is expected


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("吃饭", True),
        ("打旁边的人", True),
        ("打工", True),
        ("我去东澜港", True),
        ("我记录道路情况", True),
        ("我观察四周", True),
        ("我打算和你谈谈", False),
        ("我想打听消息", False),
        ("跟他打招呼", False),
        ("你吃了吗？", False),
        ("你什么时候休息？", False),
        ("我想问你怎么使用疗伤药？", False),
        ("我想知道怎么使用疗伤药", False),
    ],
)
def test_action_boundary_keeps_real_actions(phrase, expected) -> None:
    assert ConversationService.is_non_dialogue_intent(phrase) is expected


@pytest.mark.parametrize(
    ("phrase", "recognized"),
    [
        ("我想吃饭", True),
        ("我打算休息片刻", True),
        ("我等待30分钟", True),
        ("我原地休息", True),
        ("我准备记录道路", True),
        ("我观察四周", True),
        ("你吃了吗？", False),
        ("我想问你怎么使用疗伤药？", False),
        ("我打算和你谈谈", False),
        ("我去过东澜港", False),
    ],
)
def test_unprefixed_action_detection_ignores_questions_and_past_events(
    phrase, recognized
) -> None:
    assert (unprefixed_action_text(phrase) is not None) is recognized


@pytest.mark.parametrize(
    ("phrase", "expected_action", "expected_artifact"),
    [
        ("说话：你吃了吗？", ActionType.SOCIALIZE, "conversation"),
        ("动作：休息", ActionType.REST, "rest"),
        ("动作：开始工作", ActionType.WORK, "movement_or_work"),
        ("动作：记录道路", ActionType.ACTIVITY, "road_notes"),
    ],
)
def test_distinct_player_intents_execute_the_requested_channel(
    database, settings, dialogue_scene, phrase, expected_action, expected_artifact
) -> None:
    world_id, player_id, npc_id = dialogue_scene
    engine = WorldEngine(database, settings, decision_provider=MisclassifyingProvider())
    with database.read() as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0]
    preview = engine.preview_player_intent(world_id, phrase, target_character_id=npc_id)
    assert not preview.requires_form
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0] == before

    result = engine.submit_player_intent(world_id, phrase, target_character_id=npc_id)
    assert result.outcome.accepted
    assert result.outcome.action is expected_action
    with database.read() as connection:
        event = connection.execute(
            "SELECT event_type FROM world_events WHERE id=?", (result.outcome.event_id,)
        ).fetchone()
        assert event is not None and event["event_type"] == f"action.{expected_action.value}"
        if expected_artifact == "conversation":
            assert result.player_dialogue == "你吃了吗？"
            assert connection.execute(
                "SELECT COUNT(*) FROM npc_conversation_turns WHERE world_id=? AND message_kind='speech'",
                (world_id,),
            ).fetchone()[0] > 0
        elif expected_artifact == "rest":
            assert connection.execute(
                "SELECT 1 FROM character_life_activities WHERE character_id=? AND kind='rest' AND status='running'",
                (player_id,),
            ).fetchone() is not None
        elif expected_artifact == "movement_or_work":
            assert (
                connection.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (player_id,),
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'",
                    (player_id,),
                ).fetchone()
            )
        else:
            assert connection.execute(
                "SELECT 1 FROM player_activity_records WHERE player_id=? AND step_key='road_notes'",
                (player_id,),
            ).fetchone() is not None


class NaturalActionProvider(RuleDecisionProvider):
    name = "natural-action-test"

    def respond_to_player(self, *, npc, player, context):
        return NpcReply(reply="我听见了。")


@pytest.mark.parametrize(
    ("phrase", "expected_action"),
    [
        ("我吃饭", ActionType.EAT),
        ("我想吃饭", ActionType.EAT),
        ("我休息一下", ActionType.REST),
        ("我打算休息片刻", ActionType.REST),
        ("我原地休息", ActionType.REST),
        ("我等待30分钟", ActionType.IDLE),
        ("我去工作", ActionType.WORK),
        ("我捡起地上的铁剑", ActionType.GATHER),
        ("我使用疗伤药", ActionType.USE),
        ("我记录道路情况", ActionType.ACTIVITY),
        ("我准备记录道路", ActionType.ACTIVITY),
        ("我观察四周", ActionType.ACTIVITY),
        ("你吃了吗？", ActionType.SOCIALIZE),
        ("我想打听你最近的事", ActionType.SOCIALIZE),
    ],
)
def test_unprefixed_intents_keep_their_meaning_with_a_conversation_target(
    database, settings, dialogue_scene, phrase, expected_action
) -> None:
    world_id, player_id, npc_id = dialogue_scene
    if expected_action in {ActionType.GATHER, ActionType.USE}:
        item_name = "铁剑" if expected_action is ActionType.GATHER else "疗伤药"
        item_type_id = f"unprefixed-{expected_action.value}"
        with database.write() as connection:
            connection.execute(
                "UPDATE characters SET inventory_capacity=20 WHERE id=?", (player_id,)
            )
            connection.execute(
                "INSERT INTO item_types(id,name,category,stack_limit,usable,heal) "
                "VALUES (?,?,?,?,?,?)",
                (item_type_id, item_name,
                 "weapon" if expected_action is ActionType.GATHER else "consumable",
                 10, 1, 20 if expected_action is ActionType.USE else 0),
            )
            holder = (
                connection.execute("SELECT COALESCE(current_location_id, location_id) "
                                   "FROM characters WHERE id=?", (player_id,)).fetchone()[0]
                if expected_action is ActionType.GATHER else player_id
            )
            container = (
                "location_ground" if expected_action is ActionType.GATHER
                else "character_inventory"
            )
            connection.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_id,"
                "container_type,quantity,condition,owner_character_id) "
                "VALUES (?,?,?,?,?,1,100,?)",
                (f"unprefixed-item-{expected_action.value}", world_id, item_type_id,
                 holder, container, player_id if expected_action is ActionType.USE else None),
            )
            if expected_action is ActionType.USE:
                connection.execute("UPDATE characters SET health=40 WHERE id=?", (player_id,))
    engine = WorldEngine(database, settings, decision_provider=NaturalActionProvider())
    with database.read() as connection:
        before_events = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0]
    preview = engine.preview_player_intent(world_id, phrase, target_character_id=npc_id)
    assert not preview.requires_form
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0] == before_events
    result = engine.submit_player_intent(world_id, phrase, target_character_id=npc_id)
    assert result.outcome.action is expected_action
    assert result.outcome.accepted, result.outcome.rejection_reason
    if expected_action is ActionType.ACTIVITY:
        with database.read() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM player_activity_records WHERE world_id=? AND player_id=?",
                (world_id, player_id),
            ).fetchone()[0] >= 1
    with database.read() as connection:
        player = connection.execute(
            "SELECT satiety,health FROM characters WHERE id=?", (player_id,)
        ).fetchone()
        if expected_action is ActionType.EAT:
            assert player["satiety"] > 0
        elif expected_action is ActionType.REST:
            assert connection.execute(
                "SELECT 1 FROM character_life_activities WHERE character_id=? "
                "AND kind='rest' AND status='running'", (player_id,),
            ).fetchone() is not None
        elif expected_action is ActionType.IDLE:
            assert connection.execute(
                "SELECT 1 FROM character_life_activities WHERE character_id=? "
                "AND kind='wait' AND status='running'", (player_id,),
            ).fetchone() is not None
        elif expected_action is ActionType.WORK:
            assert connection.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (player_id,),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'",
                (player_id,),
            ).fetchone()
        elif expected_action is ActionType.GATHER:
            assert connection.execute(
                "SELECT container_id,container_type FROM item_instances WHERE id='unprefixed-item-gather'",
            ).fetchone()["container_id"] == player_id
        elif expected_action is ActionType.USE:
            assert player["health"] > 40


@pytest.mark.parametrize("travel_prefix", ["我去", "我想去"])
def test_unprefixed_named_travel_and_attack_are_not_conversation(
    database, settings, dialogue_scene, travel_prefix
) -> None:
    world_id, player_id, npc_id = dialogue_scene
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    npc = snapshot.character_by_id(npc_id)
    destination = next(item for item in snapshot.locations if item.id != npc.location_id)
    engine = WorldEngine(database, settings, decision_provider=NaturalActionProvider())
    travel = engine.submit_player_intent(
        world_id, f"{travel_prefix}{destination.name}", target_character_id=npc_id
    )
    assert travel.outcome.action is ActionType.TRAVEL
    assert travel.outcome.accepted
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND "
            "destination_location_id=? AND status='moving'",
            (player_id, destination.id),
        ).fetchone() is not None
    attack = engine.submit_player_intent(
        world_id, f"我攻击{npc.name}", target_character_id=npc_id
    )
    assert attack.outcome.action is ActionType.ATTACK
    assert attack.outcome.accepted
    with database.read() as connection:
        health = connection.execute(
            "SELECT health FROM characters WHERE id=?", (npc_id,)
        ).fetchone()[0]
        assert health < npc.health


def test_unprefixed_question_and_past_trip_do_not_consume_items_or_start_travel(
    database, settings, dialogue_scene
) -> None:
    world_id, player_id, npc_id = dialogue_scene
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
        destination = next(item for item in snapshot.locations
                           if item.id != snapshot.character_by_id(player_id).location_id)
        before_events = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0]
    engine = WorldEngine(database, settings, decision_provider=NaturalActionProvider())
    with pytest.raises(ValueError, match="选择要说话的 NPC"):
        engine.submit_player_intent(world_id, "我想知道怎么使用疗伤药")
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=?",
            (world_id, player_id),
        ).fetchone()[0] == before_events
    reply = engine.submit_player_intent(
        world_id, "我想知道怎么使用疗伤药", target_character_id=npc_id
    )
    assert reply.outcome.action is ActionType.SOCIALIZE
    past = engine.submit_player_intent(world_id, f"我去过{destination.name}")
    assert past.outcome.action is ActionType.IDLE
    with database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM character_movements WHERE character_id=?",
            (player_id,),
        ).fetchone()[0] == 0
