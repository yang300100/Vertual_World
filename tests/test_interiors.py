"""室内空间闭环：审核、门锁、权限、容量、视角与重新读取。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.clock import WorldClockService
from world_engine.conversations import ConversationService
from world_engine.database import Database
from world_engine.domain import ActionProposal, ActionType
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
