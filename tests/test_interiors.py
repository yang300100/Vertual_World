"""室内空间闭环：审核、门锁、权限、容量、视角与重新读取。"""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.clock import WorldClockService
from world_engine.conversations import ConversationService
from world_engine.database import Database
from world_engine.decisions import NpcReply
from world_engine.domain import ActionOutcome, ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.npc_performance import safe_npc_speech, settle_npc_reply
from world_engine.npc_stage_actions import travel_destinations
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import WorldRepository


@pytest.fixture
def house(settings):
    database = Database(settings.database_path)
    with TestClient(create_app(settings)) as client:
        initial = client.post("/api/worlds", json={"name": "室内测试世界"}).json()
        wid = initial["world"]["id"]
        location = initial["locations"][0]
        response = client.post(
            f"/api/worlds/{wid}/player",
            json={
                "name": "住客",
                "identity": "旅人",
                "location_id": location["id"],
            },
        )
        assert response.status_code == 201
        with database.write() as c:
            player = c.execute(
                "SELECT * FROM characters WHERE world_id=? AND is_player=1", (wid,)
            ).fetchone()
            npc = c.execute(
                "SELECT * FROM characters WHERE world_id=? AND is_player=0 LIMIT 1", (wid,)
            ).fetchone()
            c.execute("UPDATE characters SET inventory_capacity=20 WHERE id=?", (player["id"],))
        yield client, database, wid, dict(player), dict(npc), location


def layout(house, *, owner=None, policy="private", parent=None, capacity=4, name="客房"):
    client, _, wid, player, _, location = house
    body = {
        "idempotency_key": str(uuid4()),
        "room": {
            "name": name,
            "location_id": location["id"],
            "parent_room_id": parent,
            "owner_character_id": owner or player["id"],
            "access_policy": policy,
            "fixtures": [
                {"name": "木柜", "kind": "container", "capacity": capacity},
                {"name": "木椅", "kind": "seat"},
            ],
        },
    }
    response = client.post(f"/api/worlds/{wid}/interior-layouts", json=body)
    assert response.status_code == 201, response.text
    registration = response.json()["registration"]
    assert registration["status"] == "proposed"
    confirmed = client.post(f"/api/worlds/{wid}/registrations/{registration['id']}/confirm").json()
    assert confirmed["status"] == "applied", confirmed
    return confirmed["result_entity_id"], body, registration["id"]


def scene(house):
    return house[0].get(f"/api/worlds/{house[2]}/player/life").json()


def door(house, room, operation):
    return house[0].post(
        f"/api/worlds/{house[2]}/player/life/doors/{room}", json={"operation": operation}
    )


def enter(house, room):
    assert door(house, room, "open").status_code == 200
    assert door(house, room, "enter").status_code == 200


def test_unsettled_item_and_arrival_claims_are_removed_from_speech_and_stage(house):
    _, database, wid, _, npc, _ = house
    with database.write() as connection:
        actor = connection.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(connection, wid).world.current_time
        result = settle_npc_reply(
            connection, npc=actor, at=at, channel="in_person",
            reply=NpcReply(
                reply="钥匙已经交到你手里了。她已经走进屋里。我明天再给你。",
                stage_direction="她把钥匙递到你手里。随后笑了笑。",
            ),
        )
    assert result.reply == "我明天再给你。"
    assert result.stage_direction == "随后笑了笑。"
    assert safe_npc_speech("我明天再给你。") == "我明天再给你。"


def test_npc_door_action_is_settled_before_its_stage_direction_is_shown(house):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c, npc=actor, at=at, channel="in_person",
            reply=NpcReply.model_validate({
                "reply": "门被打开了。请进。",
                "stage_direction": "她推开门，朝你笑了笑。",
                "action_intent": {"kind": "door", "operation": "open", "target_id": room},
                "fallback_reply": "门打不开，等一下。",
            }),
        )
        assert c.execute("SELECT door_open FROM life_rooms WHERE id=?", (room,)).fetchone()[0] == 1
        action_event = c.execute(
            "SELECT event_type,summary FROM world_events WHERE id=?", (result.action_event_id,)
        ).fetchone()
        assert action_event["event_type"] == "action.interior_open"
        assert action_event["summary"].startswith(npc["name"])
    assert result.reply == "请进。"
    assert "打开" in result.stage_direction
    assert "推开门" not in result.stage_direction


def test_locked_door_replaces_unsettled_claim_and_uses_fallback_reply(house):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        c.execute("UPDATE life_rooms SET door_locked=1 WHERE id=?", (room,))
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c, npc=actor, at=at, channel="in_person",
            reply=NpcReply.model_validate({
                "reply": "门被打开了。请进。",
                "stage_direction": "她打开门。",
                "action_intent": {"kind": "door", "operation": "open", "target_id": room},
                "fallback_reply": "门锁着，我得先找钥匙。",
            }),
        )
        assert c.execute("SELECT door_open FROM life_rooms WHERE id=?", (room,)).fetchone()[0] == 0
    assert result.reply == "门锁着，我得先找钥匙。"
    assert "打开门" not in result.stage_direction
    assert result.action_event_id is None


def test_unsubmitted_door_claim_is_removed_without_changing_world(house):
    _, database, wid, _, npc, _ = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c,
            npc=actor,
            at=at,
            channel="in_person",
            reply=NpcReply(
                reply="我会想想办法。",
                stage_direction="门被打开了。她朝你笑了笑。",
            ),
        )
        assert c.execute("SELECT door_open FROM life_rooms WHERE id=?", (room,)).fetchone()[0] == 0
    assert result.reply == "我会想想办法。"
    assert result.stage_direction == "她朝你笑了笑。"


def test_remote_reply_cannot_open_a_door_or_show_local_stage(house):
    _, database, wid, _, npc, _ = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c,
            npc=actor,
            at=at,
            channel="letter",
            reply=NpcReply.model_validate({
                "reply": "请进。",
                "stage_direction": "她朝你笑了笑。",
                "action_intent": {"kind": "door", "operation": "open", "target_id": room},
                "fallback_reply": "等见面再说。",
            }),
        )
        assert c.execute("SELECT door_open FROM life_rooms WHERE id=?", (room,)).fetchone()[0] == 0
    assert result.reply == "等见面再说。"
    assert result.stage_direction is None


def test_npc_can_enter_a_room_only_after_door_rules_allow_it(house):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        c.execute("UPDATE life_rooms SET door_open=1 WHERE id=?", (room,))
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c, npc=actor, at=at, channel="in_person",
            reply=NpcReply.model_validate({
                "reply": "我进去看看。",
                "action_intent": {"kind": "room", "operation": "enter", "target_id": room},
                "fallback_reply": "我现在还进不去。",
            }),
        )
        current = c.execute(
            "SELECT current_room_id FROM characters WHERE id=?", (npc["id"],)
        ).fetchone()
        assert current[0] == room
    assert result.action_status == "applied"
    assert "进入" in result.stage_direction


def test_npc_gift_uses_owned_inventory_capacity_and_audit(house):
    _, database, wid, player, npc, location = house
    item_id = str(uuid4())
    item_type = str(uuid4())
    with database.write() as c:
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,?,?,?)",
            (item_type, "铜钥匙", "tool", 10),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,"
            "quantity,condition,owner_character_id) VALUES (?,?,?,?,'character_inventory',2,100,?)",
            (item_id, wid, item_type, npc["id"], npc["id"]),
        )
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c, npc=actor, player_id=player["id"], at=at, channel="in_person",
            reply=NpcReply.model_validate({
                "reply": "这把给你。",
                "action_intent": {"kind": "give_item", "target_id": item_id, "quantity": 1},
                "fallback_reply": "我现在没法给你。",
            }),
        )
        given = c.execute(
            "SELECT quantity,owner_character_id FROM item_instances WHERE container_id=? "
            "AND item_type_id=?", (player["id"], item_type),
        ).fetchone()
        assert tuple(given) == (1, player["id"])
        assert c.execute(
            "SELECT COUNT(*) FROM inventory_changes WHERE source_event_id=?",
            (result.action_event_id,),
        ).fetchone()[0] > 0
    assert result.action_status == "applied"
    assert "交给" in result.stage_direction


def test_npc_cannot_give_an_item_it_does_not_own(house):
    _, database, wid, player, npc, _ = house
    with database.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c, npc=actor, player_id=player["id"], at=at, channel="in_person",
            reply=NpcReply.model_validate({
                "reply": "东西给你。",
                "action_intent": {"kind": "give_item", "target_id": "missing-item"},
                "fallback_reply": "我现在拿不出来。",
            }),
        )
    assert result.action_status == "rejected"
    assert result.reply == "我现在拿不出来。"
    assert result.action_event_id is None


def test_npc_travel_starts_a_real_journey_without_claiming_arrival(house):
    _, database, wid, player, npc, location = house
    with database.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        destination = travel_destinations(c, actor)[0]
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        result = settle_npc_reply(
            c, npc=actor, at=at, channel="in_person",
            reply=NpcReply.model_validate({
                "reply": "我先出发，到了再告诉你。",
                "action_intent": {"kind": "travel", "target_id": destination["id"]},
                "fallback_reply": "我现在还走不开。",
            }),
        )
        movement = c.execute(
            "SELECT status,destination_location_id FROM character_movements "
            "WHERE character_id=? ORDER BY created_at DESC LIMIT 1", (npc["id"],),
        ).fetchone()
        assert tuple(movement) == ("moving", destination["id"])
    assert result.action_status == "applied"
    assert "尚未到达" in result.stage_direction


def test_dialogue_commits_npc_door_action_and_only_records_settled_text(house, settings):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )

    class Provider:
        name = "test-npc"
        context = None

        def respond_to_player(self, *, npc, player, context):
            self.context = context
            return NpcReply.model_validate({
                "reply": "门被打开了。请进。",
                "stage_direction": "她望着你，推开门。",
                "action_intent": {"kind": "door", "operation": "open", "target_id": room},
                "fallback_reply": "门打不开，稍等。",
            })

    provider = Provider()
    result = WorldEngine(database, settings, decision_provider=provider).submit_player_intent(
        wid, "说话：请帮我开门。", target_character_id=npc["id"]
    )
    assert result.outcome.accepted
    assert result.npc_reply == "请进。"
    assert result.player_dialogue == "请帮我开门。"
    assert result.npc_stage_direction and "打开" in result.npc_stage_direction
    assert "门被打开了" not in result.outcome.summary
    assert provider.context["scene"]["doors"][0]["id"] == room
    assert provider.context["scene"]["inventory"] == []
    assert provider.context["scene"]["travel_destinations"]
    with database.read() as c:
        assert c.execute("SELECT door_open FROM life_rooms WHERE id=?", (room,)).fetchone()[0] == 1
        version = c.execute("SELECT version FROM worlds WHERE id=?", (wid,)).fetchone()[0]
        assert result.current_version == version
        event = c.execute(
            "SELECT payload_json FROM world_events WHERE id=?", (result.outcome.event_id,)
        ).fetchone()
        payload = json.loads(event[0])
        assert payload["reply"] == "请进。"
        assert "打开" in payload["metadata"]["npc_stage_direction"]


def test_rejected_dialogue_does_not_commit_npc_door_action(house, settings):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )

    class Provider:
        name = "test-npc"

        def respond_to_player(self, *, npc, player, context):
            return NpcReply.model_validate({
                "reply": "请进。",
                "action_intent": {"kind": "door", "operation": "open", "target_id": room},
            })

    engine = WorldEngine(database, settings, decision_provider=Provider())
    engine.actions.execute = lambda *args, **kwargs: ActionOutcome(
        accepted=False,
        actor_id=player["id"],
        action=ActionType.SOCIALIZE,
        summary="对话被拒绝",
        rejection_reason="对话被拒绝",
    )
    result = engine.submit_player_intent(
        wid, "说话：请帮我开门。", target_character_id=npc["id"]
    )
    assert not result.outcome.accepted
    assert result.npc_reply is None
    with database.read() as c:
        assert c.execute("SELECT door_open FROM life_rooms WHERE id=?", (room,)).fetchone()[0] == 0


def test_dialogue_can_finish_before_npc_enters_another_room(house, settings):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, owner=npc["id"], policy="private")
    with database.write() as c:
        c.execute("UPDATE life_rooms SET door_open=1 WHERE id=?", (room,))
        c.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=? WHERE id=?",
            (player["longitude"], player["latitude"], location["id"], npc["id"]),
        )

    class Provider:
        name = "test-npc"

        def respond_to_player(self, *, npc, player, context):
            return NpcReply.model_validate({
                "reply": "我进去看看。",
                "action_intent": {"kind": "room", "operation": "enter", "target_id": room},
                "fallback_reply": "现在进不去。",
            })

    result = WorldEngine(database, settings, decision_provider=Provider()).submit_player_intent(
        wid, "说话：你先进去看看。", target_character_id=npc["id"]
    )
    assert result.outcome.accepted
    assert result.npc_reply == "我进去看看。"
    assert "进入" in result.npc_stage_direction
    with database.read() as c:
        assert c.execute(
            "SELECT current_room_id FROM characters WHERE id=?", (npc["id"],)
        ).fetchone()[0] == room
        payload = json.loads(c.execute(
            "SELECT payload_json FROM world_events WHERE id=?", (result.outcome.event_id,)
        ).fetchone()[0])
        assert payload["reply"] == result.npc_reply
        assert payload["metadata"]["npc_action_status"] == "applied"
        memories = c.execute(
            "SELECT summary FROM character_memories WHERE event_id=?",
            (result.outcome.event_id,),
        ).fetchall()
        assert memories and all("我进去看看" in row[0] for row in memories)


def test_group_dialogue_commits_room_move_after_each_social_event(house, settings):
    _, database, wid, player, npc, location = house
    room, _, _ = layout(house, policy="public")
    with database.write() as connection:
        connection.execute("UPDATE life_rooms SET door_open=1 WHERE id=?", (room,))
        other = connection.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (wid, npc["id"]),
        ).fetchone()[0]
        connection.execute(
            "UPDATE characters SET longitude=?,latitude=?,current_location_id=?,"
            "current_room_id=NULL "
            "WHERE id IN (?,?)",
            (player["longitude"], player["latitude"], location["id"], npc["id"], other),
        )

    class Provider:
        name = "test-group-npc"
        count = 0

        def respond_to_player(self, *, npc, player, context):
            self.count += 1
            if self.count == 1:
                return NpcReply.model_validate({
                    "reply": "我进去看看。",
                    "action_intent": {"kind": "room", "operation": "enter", "target_id": room},
                    "fallback_reply": "我还进不去。",
                })
            return NpcReply(reply="我在这里等你。")

    result = WorldEngine(database, settings, decision_provider=Provider()).submit_group_dialogue(
        wid, "你们先商量一下。", participant_ids=[npc["id"], other], max_speakers=2,
    )
    assert len(result["replies"]) == 2
    assert result["replies"][0]["reply"] == "我进去看看。"
    assert "进入" in result["replies"][0]["stage_direction"]
    with database.read() as connection:
        moved_id = result["replies"][0]["character_id"]
        assert connection.execute(
            "SELECT current_room_id FROM characters WHERE id=?", (moved_id,),
        ).fetchone()[0] == room
        for item in result["replies"]:
            row = connection.execute(
                "SELECT summary,payload_json FROM world_events WHERE id=?", (item["event_id"],),
            ).fetchone()
            payload = json.loads(row["payload_json"])
            assert payload["reply"] == item["reply"]
            assert item["reply"] in row["summary"]
        assert connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND event_type='action.socialize' "
            "AND tick_id=?", (wid, f"group-dialogue:{result['group_dialogue_id']}"),
        ).fetchone()[0] == 2


def test_layout_requires_review_and_cannot_be_applied_by_character(house):
    client, database, wid, player, _, location = house
    body = {
        "idempotency_key": "review-interior",
        "room": {"name": "待审核房间", "location_id": location["id"]},
    }
    response = client.post(f"/api/worlds/{wid}/interior-layouts", json=body)
    assert response.status_code == 201
    assert scene(house)["interior"]["doors"] == []
    repeated = client.post(f"/api/worlds/{wid}/interior-layouts", json=body)
    assert repeated.json()["registration"]["id"] == response.json()["registration"]["id"]
    with database.write() as c:
        from world_engine.actions import ActionService

        snapshot = WorldRepository().get_snapshot(c, wid)
        source = (
            ActionService()
            .execute(
                c,
                world_id=wid,
                tick_id=str(uuid4()),
                occurred_at=snapshot.world.current_time,
                proposal=ActionProposal(
                    actor_id=player["id"], action=ActionType.IDLE, reason="核实布局"
                ),
            )
            .event_id
        )
        result = WorldElementRegistry().submit(
            c,
            world_id=wid,
            request=ElementRegistrationSubmit(
                source_event_id=source,
                requested_by_character_id=player["id"],
                idempotency_key="character-fake-room",
                payload={
                    "element_type": "interior_room",
                    "name": "凭空房产",
                    "location_id": location["id"],
                },
            ),
        )
        assert result.status == "rejected"
        assert c.execute("SELECT count(*) FROM life_rooms").fetchone()[0] == 0


def test_doors_seats_and_state_survive_leaving_and_reinitializing(house):
    client, database, wid, _, _, _ = house
    room, _, _ = layout(house)
    assert door(house, room, "enter").status_code == 409
    enter(house, room)
    interior = scene(house)["interior"]
    seat = next(item for item in interior["fixtures"] if item["kind"] == "seat")
    route = f"/api/worlds/{wid}/player/life/fixtures/{seat['id']}"
    assert client.post(route, json={"operation": "sit"}).status_code == 200
    assert door(house, room, "exit").status_code == 409
    database.initialize()
    assert scene(house)["interior"]["seated"]
    assert client.post(route, json={"operation": "stand"}).status_code == 200
    assert door(house, room, "close").status_code == 200
    assert door(house, room, "lock").status_code == 200
    assert door(house, room, "open").status_code == 409
    assert door(house, room, "unlock").status_code == 200
    assert door(house, room, "open").status_code == 200
    assert door(house, room, "exit").status_code == 200
    assert scene(house)["interior"]["room"] is None
    enter(house, room)
    with database.read() as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_private_room_permissions_and_inside_escape(house):
    client, database, wid, player, npc, _ = house
    room, _, _ = layout(house, owner=npc["id"])
    assert door(house, room, "open").status_code == 409
    with database.write() as c:
        c.execute("INSERT INTO life_room_access VALUES (?,?)", (room, player["id"]))
    enter(house, room)
    with database.write() as c:
        c.execute("DELETE FROM life_room_access WHERE room_id=?", (room,))
        c.execute("UPDATE life_rooms SET door_open=0,door_locked=1 WHERE id=?", (room,))
    assert door(house, room, "unlock").status_code == 200
    assert door(house, room, "open").status_code == 200
    assert door(house, room, "exit").status_code == 200
    assert door(house, room, "enter").status_code == 409


def test_nested_room_does_not_allow_skipping_parent_or_accessing_remote_fixtures(house):
    client, _, wid, _, _, _ = house
    outer, _, _ = layout(house, name="前厅")
    inner, _, _ = layout(house, parent=outer, name="里间")
    assert door(house, inner, "open").status_code == 409
    enter(house, outer)
    enter(house, inner)
    fixture = scene(house)["interior"]["fixtures"][0]
    assert door(house, outer, "exit").status_code == 409
    assert door(house, inner, "exit").status_code == 200
    assert scene(house)["interior"]["room"]["id"] == outer
    result = client.post(
        f"/api/worlds/{wid}/player/life/fixtures/{fixture['id']}", json={"operation": "open"}
    )
    assert result.status_code == 409


def test_storage_capacity_quantity_ownership_and_closed_visibility(house):
    client, database, wid, player, npc, _ = house
    room, _, _ = layout(house, capacity=1)
    enter(house, room)
    fixture = next(
        item for item in scene(house)["interior"]["fixtures"] if item["kind"] == "container"
    )
    base = f"/api/worlds/{wid}/player/life/fixtures/{fixture['id']}"
    with database.write() as c:
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit) "
            "VALUES ('interior-paper','稿纸','material',10)"
        )
        c.execute(
            "INSERT INTO "
            "item_instances(id,world_id,item_type_id,container_type,container_id,"
            "quantity,condition,owner_character_id) "
            "VALUES ('paper',?,'interior-paper','character_inventory',?,8,90,?)",
            (wid, player["id"], player["id"]),
        )
    body = {"item_id": "paper", "quantity": 3, "operation": "deposit"}
    assert client.post(base + "/items", json=body).status_code == 409
    assert client.post(base, json={"operation": "open"}).status_code == 200
    assert client.post(base + "/items", json=body).status_code == 200
    assert client.post(base + "/items", json={**body, "quantity": 5}).status_code == 200
    visible = scene(house)["interior"]["fixtures"]
    contents = next(item for item in visible if item["id"] == fixture["id"])["contents"]
    assert len(contents) == 1 and contents[0]["quantity"] == 8
    other_item = next(item for item in scene(house)["items"] if item["place"] == "bag")
    overfull = client.post(
        base + "/items",
        json={
            "item_id": other_item["id"],
            "quantity": 1,
            "operation": "deposit",
        },
    )
    assert overfull.status_code == 409 and "容器空间不足" in overfull.text
    assert (
        client.get(f"/api/worlds/{wid}/player/life/items/{other_item['id']}").json()["quantity"]
        == other_item["quantity"]
    )
    assert client.get(f"/api/worlds/{wid}/player/life/items/{contents[0]['id']}").status_code == 404
    assert client.post(base, json={"operation": "close"}).status_code == 200
    assert all(not item["contents"] for item in scene(house)["interior"]["fixtures"])
    assert client.post(base, json={"operation": "open"}).status_code == 200
    with database.write() as c:
        c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (player["id"],))
    take = {"item_id": contents[0]["id"], "quantity": 2, "operation": "withdraw"}
    assert client.post(base + "/items", json=take).status_code == 409
    with database.write() as c:
        c.execute("UPDATE characters SET inventory_capacity=20 WHERE id=?", (player["id"],))
    assert client.post(base + "/items", json=take).status_code == 200
    with database.write() as c:
        remaining = c.execute(
            "SELECT * FROM item_instances WHERE container_id=? AND "
            "container_type='fixture_storage'",
            (fixture["id"],),
        ).fetchone()
        assert remaining["quantity"] == 6 and remaining["owner_character_id"] == player["id"]
        c.execute(
            "UPDATE item_instances SET owner_character_id=? WHERE id=?",
            (npc["id"], remaining["id"]),
        )
    assert client.post(base + "/items", json=take).status_code == 409


def test_room_ground_cannot_be_seen_or_taken_outside(house):
    client, _, wid, _, _, _ = house
    room, _, _ = layout(house)
    item = scene(house)["items"][0]
    enter(house, room)
    url = f"/api/worlds/{wid}/player/life/items/{item['id']}"
    assert client.post(url, json={"operation": "drop"}).status_code == 200
    assert client.get(url).status_code == 200
    assert door(house, room, "exit").status_code == 200
    assert client.get(url).status_code == 404
    enter(house, room)
    assert client.post(url, json={"operation": "pickup"}).status_code == 200


def test_walls_block_dialogue_contacts_combat_and_outdoor_movement(house):
    from world_engine.actions import ActionService
    from world_engine.combat import CombatResolver

    client, database, wid, player, npc, location = house
    room, _, _ = layout(house)
    enter(house, room)
    with database.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (player["longitude"], player["latitude"], npc["id"]),
        )
        snapshot = WorldRepository().get_snapshot(c, wid)
        with pytest.raises(ValueError):
            ConversationService().resolve_target(
                c,
                snapshot=snapshot,
                player=snapshot.character_by_id(player["id"]),
                intent="你好",
                target_character_id=npc["id"],
            )
        for kind in (ActionType.ATTACK, ActionType.SOCIALIZE):
            proposal = ActionProposal(
                actor_id=player["id"], target_id=npc["id"], action=kind, reason="隔墙测试"
            )
            result = ActionService().execute(
                c,
                world_id=wid,
                tick_id=str(uuid4()),
                occurred_at=snapshot.world.current_time,
                proposal=proposal,
            )
            assert not result.accepted
        result = CombatResolver().resolve_proposal(
            c,
            world_id=wid,
            tick_id=str(uuid4()),
            occurred_at=snapshot.world.current_time,
            proposal=ActionProposal(
                actor_id=player["id"], target_id=npc["id"], action=ActionType.ATTACK, reason="隔墙"
            ),
        )
        assert not result.outcome.accepted
    assert (
        client.post(f"/api/worlds/{wid}/contacts", json={"recipient_id": npc["id"]}).status_code
        == 403
    )
    move = client.post(
        f"/api/worlds/{wid}/player/move",
        json={
            "destination_longitude": location["longitude"] + 1,
            "destination_latitude": location["latitude"],
        },
    )
    assert move.status_code == 400


def test_heartbeat_keeps_room_and_life_activity_prevents_leaving(house, settings):
    client, database, wid, player, _, _ = house
    room, _, _ = layout(house)
    enter(house, room)
    assert (
        client.post(
            f"/api/worlds/{wid}/player/life/activities",
            json={"kind": "rest", "duration_minutes": 30},
        ).status_code
        == 201
    )
    assert door(house, room, "exit").status_code == 409
    WorldClockService(database, settings).heartbeat(wid, elapsed_seconds=600)
    assert scene(house)["interior"]["room"]["id"] == room
    assert scene(house)["activity"]["progress"] == pytest.approx(1 / 3)


def test_foreign_world_layout_owner_is_rejected(house):
    client, _, wid, _, _, location = house
    response = client.post(
        f"/api/worlds/{wid}/interior-layouts",
        json={
            "idempotency_key": "foreign-owner",
            "room": {
                "name": "无效房间",
                "location_id": location["id"],
                "owner_character_id": "not-in-world",
                "access_policy": "private",
            },
        },
    ).json()
    result = client.post(
        f"/api/worlds/{wid}/registrations/{response['registration']['id']}/confirm"
    ).json()
    assert result["status"] == "rejected"
    assert scene(house)["interior"]["doors"] == []


def test_owner_can_grant_and_revoke_access_but_cannot_manage_someone_elses_room(house):
    client, database, wid, _, npc, _ = house
    room, _, _ = layout(house)
    route = f"/api/worlds/{wid}/player/life/rooms/{room}/access"
    for allow in (True, False):
        result = client.post(route, json={"character_id": npc["id"], "allow": allow})
        assert result.status_code == 200
        with database.read() as c:
            granted = c.execute(
                "SELECT 1 FROM life_room_access WHERE room_id=? AND character_id=?",
                (room, npc["id"]),
            ).fetchone()
            assert bool(granted) is allow
    other_room, _, _ = layout(house, owner=npc["id"], name="邻居的房间")
    result = client.post(
        f"/api/worlds/{wid}/player/life/rooms/{other_room}/access",
        json={"character_id": npc["id"], "allow": True},
    )
    assert result.status_code == 403
