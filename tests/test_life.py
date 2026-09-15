"""生活第一阶段：世界时间、互斥、原地物品与持久化的行为回归。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.clock import WorldClockService
from world_engine.database import Database
from world_engine.life import LifeActivityService, parse_life_activity
from world_engine.repository import from_iso


@pytest.fixture
def life_world(settings):
    database = Database(settings.database_path)
    with TestClient(create_app(settings)) as client:
        world = client.post("/api/worlds", json={"name": "生活回归世界"}).json()
        wid = world["world"]["id"]
        location = world["locations"][0]
        player_response = client.post(
            f"/api/worlds/{wid}/player",
            json={
                "name": "生活测试旅人",
                "identity": "旅人",
                "location_id": location["id"],
            },
        )
        assert player_response.status_code == 201
        with database.write() as connection:
            player = connection.execute(
                "SELECT * FROM characters WHERE world_id=? AND is_player=1",
                (wid,),
            ).fetchone()
            connection.execute(
                "UPDATE characters SET energy=30,satiety=80 WHERE id=?", (player["id"],)
            )
        yield client, database, wid, player["id"], location, WorldClockService(database, settings)


def _start(client, wid, kind="rest", duration=60):
    return client.post(
        f"/api/worlds/{wid}/player/life/activities",
        json={
            "kind": kind,
            "duration_minutes": duration,
        },
    )


def _energy(database, player):
    with database.read() as connection:
        return connection.execute("SELECT energy FROM characters WHERE id=?", (player,)).fetchone()[
            0
        ]


def test_rest_progresses_with_time_and_completes_exactly_once(life_world):
    client, database, wid, player, _, clock = life_world
    started = _start(client, wid)
    assert started.status_code == 201
    assert _energy(database, player) == 30
    clock.heartbeat(wid, elapsed_seconds=30 * 60)
    assert _energy(database, player) == 33
    scene = client.get(f"/api/worlds/{wid}/player/life").json()
    assert scene["activity"]["progress"] == 0.5
    clock.heartbeat(wid, elapsed_seconds=30 * 60)
    assert _energy(database, player) == 37
    clock.heartbeat(wid, elapsed_seconds=0)
    assert _energy(database, player) == 37
    scene = client.get(f"/api/worlds/{wid}/player/life").json()
    assert scene["activity"] is None
    assert scene["last_activity"]["status"] == "completed"
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM world_events WHERE world_id=? "
                "AND event_type='action.life_completed'",
                (wid,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM character_state_updates WHERE character_id=? "
                "AND cause='rest_time_passage'",
                (player,),
            ).fetchone()[0]
            == 2
        )


def test_cancel_preserves_partial_recovery_and_is_idempotent(life_world):
    client, database, wid, player, _, clock = life_world
    aid = _start(client, wid).json()["activity"]["id"]
    clock.heartbeat(wid, elapsed_seconds=30 * 60)
    for _ in range(2):
        stopped = client.post(f"/api/worlds/{wid}/player/life/activities/{aid}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["activity"]["status"] == "cancelled"
    assert _energy(database, player) == 33
    clock.heartbeat(wid, elapsed_seconds=30 * 60)
    assert _energy(database, player) == 34
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM world_events WHERE world_id=? "
                "AND event_type='action.life_cancelled'",
                (wid,),
            ).fetchone()[0]
            == 1
        )


def test_pause_and_time_scale_settlement_apply_to_life(life_world):
    client, database, wid, player, _, clock = life_world
    clock.reset_offline_baseline()
    with database.read() as connection:
        baseline = from_iso(
            connection.execute(
                "SELECT last_heartbeat_real_time FROM world_clock WHERE world_id=?",
                (wid,),
            ).fetchone()[0]
        )
    _start(client, wid)
    clock.set_time_scale(wid, 0, real_now=baseline + timedelta(minutes=30))
    assert _energy(database, player) == 33
    clock.heartbeat(wid, elapsed_seconds=3600)
    assert _energy(database, player) == 33
    assert client.get(f"/api/worlds/{wid}/player/life").json()["activity"]["progress"] == 0.5


def test_activity_survives_database_reinitialization(life_world):
    client, database, wid, player, _, clock = life_world
    aid = _start(client, wid).json()["activity"]["id"]
    clock.heartbeat(wid, elapsed_seconds=600)
    database.initialize()
    with database.read() as connection:
        assert LifeActivityService.running(connection, player)["id"] == aid
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_wait_has_no_extra_rest_reward_and_conflicting_actions_are_rejected(life_world):
    client, database, wid, player, location, clock = life_world
    assert _start(client, wid, "wait", 60).status_code == 201
    assert _start(client, wid).status_code == 409
    move = client.post(
        f"/api/worlds/{wid}/player/move",
        json={
            "destination_longitude": location["longitude"] + 1,
            "destination_latitude": location["latitude"],
        },
    )
    assert move.status_code == 400
    action = client.post(f"/api/worlds/{wid}/player/act", json={"intent": "动作：记录道路"})
    assert not action.json()["outcome"]["accepted"]
    clock.heartbeat(wid, elapsed_seconds=3600)
    assert _energy(database, player) == 32


@pytest.mark.parametrize("changed", ["health", "longitude"])
def test_damage_or_displacement_interrupts_without_granting_unobserved_time(life_world, changed):
    client, database, wid, player, _, clock = life_world
    _start(client, wid)
    with database.write() as connection:
        if changed == "health":
            connection.execute("UPDATE characters SET health=health-1 WHERE id=?", (player,))
        else:
            connection.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (player,))
    clock.heartbeat(wid, elapsed_seconds=600)
    scene = client.get(f"/api/worlds/{wid}/player/life").json()
    assert scene["activity"] is None
    assert scene["last_activity"]["status"] == "interrupted"
    assert _energy(database, player) == 30


@pytest.mark.parametrize("duration", [0, 721, -1, 1.5, True])
def test_invalid_durations_do_not_start_activity(life_world, duration):
    client, database, wid, player, _, _ = life_world
    assert _start(client, wid, duration=duration).status_code == 422
    with database.read() as connection:
        assert LifeActivityService.running(connection, player) is None


def _items(database, wid, player, location):
    with database.write() as connection:
        connection.execute("UPDATE characters SET inventory_capacity=20 WHERE id=?", (player,))
        connection.execute(
            "INSERT INTO item_types(id,name,category) VALUES ('life-type','测试旧图','map')"
        )
        other = connection.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 LIMIT 1",
            (wid,),
        ).fetchone()[0]
        for iid, holder, container, owner in [
            ("owned-ground", location["id"], "location_ground", player),
            ("other-ground", location["id"], "location_ground", other),
            ("hidden-item", other, "character_inventory", other),
        ]:
            connection.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,"
                "quantity,condition,owner_character_id) VALUES (?,?,'life-type',?,?,1,90,?)",
                (iid, wid, holder, container, owner),
            )


def test_scene_item_identity_ownership_and_persistent_drop(life_world):
    client, database, wid, player, location, _ = life_world
    _items(database, wid, player, location)
    base = f"/api/worlds/{wid}/player/life"
    items = client.get(base).json()["items"]
    assert {item["id"] for item in items if item["place"] == "ground"} == {
        "owned-ground", "other-ground",
    }
    assert client.get(base + "/items/hidden-item").status_code == 404
    assert (
        client.post(base + "/items/other-ground", json={"operation": "pickup"}).status_code == 409
    )
    assert (
        client.post(base + "/items/owned-ground", json={"operation": "pickup"}).status_code == 200
    )
    assert (
        client.post(base + "/items/owned-ground", json={"operation": "pickup"}).status_code == 409
    )
    assert client.post(base + "/items/owned-ground", json={"operation": "drop"}).status_code == 200
    database.initialize()
    item = client.get(base + "/items/owned-ground").json()
    assert item["place"] == "ground" and item["ownership"] == "self"
    with database.read() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM inventory_changes WHERE world_id=?",
                (wid,),
            ).fetchone()[0]
            == 2
        )


def test_item_location_and_capacity_are_revalidated(life_world):
    client, database, wid, player, location, _ = life_world
    _items(database, wid, player, location)
    base = f"/api/worlds/{wid}/player/life"
    with database.write() as connection:
        connection.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (player,))
    assert (
        client.post(base + "/items/owned-ground", json={"operation": "pickup"}).status_code == 409
    )
    with database.write() as connection:
        connection.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (player,))
    assert all(item["place"] == "bag" for item in client.get(base).json()["items"])
    bag_item = client.get(base).json()["items"][0]
    assert bag_item["actions"] == ["inspect"]
    assert client.post(
        base + f"/items/{bag_item['id']}", json={"operation": "drop"},
    ).status_code == 409
    assert (
        client.post(base + "/items/owned-ground", json={"operation": "pickup"}).status_code == 404
    )


def test_explicit_duration_parser_keeps_single_action_boundary():
    assert parse_life_activity("休息2小时") == ("rest", 120)
    assert parse_life_activity("等待 10 分钟") == ("wait", 10)
    assert parse_life_activity("我问他是否要休息") is None
    assert parse_life_activity("休息30分钟，然后去工作") is None
