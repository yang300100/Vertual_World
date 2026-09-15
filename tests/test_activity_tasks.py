# ruff: noqa: F811
# pytest 按名称注入跨模块复用的 fixture，测试参数会使用相同名称。
"""持续活动与主动等候的事务、时序和库存回归。"""

import json
from datetime import timedelta
from uuid import uuid4

from test_life import life_world  # noqa: F401

from world_engine.repository import from_iso, to_iso


def start_work(env):
    client, _, wid, _, _, _ = env
    response = client.post(f"/api/worlds/{wid}/player/life/tasks", json={})
    assert response.status_code == 201, response.text
    return response.json()["activity_id"]


def wait_request(env, aid):
    client, database, wid, _, _, _ = env
    with database.write() as c:
        at = from_iso(
            c.execute('SELECT "current_time" FROM worlds WHERE id=?', (wid,)).fetchone()[0]
        )
        c.execute(
            "UPDATE world_clock SET next_adjudication_world_time=? WHERE world_id=?",
            (to_iso(at + timedelta(hours=6)), wid),
        )
    return {
        "expected_version": client.get(f"/api/worlds/{wid}").json()["world"]["version"],
        "request_id": str(uuid4()),
    }


def register_recipe(env, kind="craft", duration=30):
    client, database, wid, player, location, _ = env
    with database.write() as c:
        c.execute("UPDATE characters SET inventory_capacity=20,energy=80 WHERE id=?", (player,))
        c.execute(
            'INSERT INTO item_types(id,name,category,stack_limit) VALUES '
            "('task-material','修补材料','material',10)"
        )
        c.execute(
            'INSERT INTO item_types(id,name,category,stack_limit) VALUES '
            "('task-product','修好器具','tool',10)"
        )
        c.execute(
            'INSERT INTO item_instances(id,world_id,item_type_id,containe'
            'r_type,container_id,quantity,condition,owner_character_id) V'
            "ALUES ('material',?,'task-material','character_inventory',?,"
            '5,100,?)',
            (wid, player, player),
        )
        c.execute(
            'INSERT INTO item_instances(id,world_id,item_type_id,containe'
            'r_type,container_id,quantity,condition,owner_character_id) V'
            "ALUES ('repair-target',?,'task-product','character_inventory"
            "',?,1,50,?)",
            (wid, player, player),
        )
    spec = {
        "name": "手工制作" if kind == "craft" else "器具修理",
        "kind": kind,
        "location_id": location["id"],
        "duration_minutes": duration,
        "energy_cost": 10,
        "ingredients": [{"item_type_id": "task-material", "quantity": 2}],
        "output_item_type_id": "task-product" if kind == "craft" else None,
        "output_quantity": 3,
        "repair_category": "tool" if kind == "repair" else None,
        "repair_points": 20,
    }
    proposed = client.post(
        f"/api/worlds/{wid}/activity-recipes",
        json={"idempotency_key": str(uuid4()), "recipe": spec},
    )
    assert proposed.status_code == 201, proposed.text
    rid = proposed.json()["registration"]["id"]
    assert client.get(f"/api/worlds/{wid}/player/life").json()["recipes"] == []
    approved = client.post(f"/api/worlds/{wid}/registrations/{rid}/confirm").json()
    assert approved["status"] == "applied", approved
    return approved["result_entity_id"]


def test_work_pays_only_after_duration_and_stopping_preserves_effort(life_world):
    client, database, wid, player, _, clock = life_world
    before = client.get(f"/api/worlds/{wid}").json()
    money = next(item["money"] for item in before["characters"] if item["is_player"])
    aid = start_work(life_world)
    clock.heartbeat(wid, elapsed_seconds=1800)
    with database.read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (player,)).fetchone()
        assert actor["money"] == money and actor["energy"] == 23
    client.post(f"/api/worlds/{wid}/player/life/activities/{aid}/stop")
    clock.heartbeat(wid, elapsed_seconds=1800)
    with database.read() as c:
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (player,)).fetchone()[0] == money
        )
    aid = start_work(life_world)
    clock.heartbeat(wid, elapsed_seconds=3600)
    clock.heartbeat(wid, elapsed_seconds=0)
    with database.read() as c:
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (player,)).fetchone()[0]
            == money + 9
        )
        events = c.execute(
            "SELECT payload_json FROM world_events WHERE world_id=? AND actor_id=? AND event_type='action.work'",
            (wid,player),
        ).fetchall()
        assert sum(bool(json.loads(row[0]).get("work_completed")) for row in events) == 1


def test_time_skip_is_atomic_idempotent_and_works_while_paused(life_world):
    client, database, wid, _, _, _ = life_world
    aid = start_work(life_world)
    body = wait_request(life_world, aid)
    with database.write() as c:
        c.execute("UPDATE world_clock SET time_scale=0 WHERE world_id=?", (wid,))
    url = f"/api/worlds/{wid}/player/life/activities/{aid}/advance"
    first = client.post(url, json=body)
    assert first.status_code == 200, first.text
    second = client.post(url, json=body)
    assert second.status_code == 200
    assert second.json()["heartbeat"]["time_skip_replayed"]
    assert first.json()["heartbeat"]["heartbeat_id"] == second.json()["heartbeat"]["heartbeat_id"]
    assert first.json()["heartbeat"]["world_delta_seconds"] == 3600
    assert client.get(f"/api/worlds/{wid}/player/life").json()["activity"] is None
    with database.read() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM activity_time_skips WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute("SELECT time_scale FROM world_clock WHERE world_id=?", (wid,)).fetchone()[0]
            == 0
        )


def test_time_skip_rejects_stale_snapshot_and_stops_at_adjudication(life_world):
    client, database, wid, _, _, _ = life_world
    response = client.post(
        f"/api/worlds/{wid}/player/life/activities", json={"kind": "rest", "duration_minutes": 30}
    )
    aid = response.json()["activity"]["id"]
    body = wait_request(life_world, aid)
    url = f"/api/worlds/{wid}/player/life/activities/{aid}/advance"
    with database.write() as c:
        at = from_iso(
            c.execute('SELECT "current_time" FROM worlds WHERE id=?', (wid,)).fetchone()[0]
        )
        c.execute("UPDATE worlds SET version=version+1 WHERE id=?", (wid,))
        c.execute(
            "UPDATE world_clock SET next_adjudication_world_time=? WHERE world_id=?",
            (to_iso(at + timedelta(minutes=10)), wid),
        )
    assert client.post(url, json=body).status_code == 409
    with database.read() as c:
        assert (
            from_iso(
                c.execute('SELECT "current_time" FROM worlds WHERE id=?', (wid,)).fetchone()[0]
            )
            == at
        )
    body["expected_version"] += 1
    result = client.post(url, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["heartbeat"]["world_delta_seconds"] == 600
    assert result.json()["heartbeat"]["time_skip_reason"] == "下一次世界裁判"


def test_craft_reserves_materials_and_produces_once_with_safe_claim(life_world):
    client, database, wid, player, _, clock = life_world
    recipe = register_recipe(life_world)
    response = client.post(f"/api/worlds/{wid}/player/life/tasks", json={"recipe_id": recipe})
    assert response.status_code == 201, response.text
    aid = response.json()["activity_id"]
    with database.read() as c:
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id='material'").fetchone()[0] == 3
        )
    assert client.get(f"/api/worlds/{wid}/player/life").json()["pending_outputs"] == []
    database.initialize()
    active = client.get(f"/api/worlds/{wid}/player/life").json()["activity"]
    assert active["title"] == "手工制作"
    assert active["task_plan"]["output_quantity"] == 3
    assert "reserved_items" not in active["task_plan"]
    clock.heartbeat(wid, elapsed_seconds=1800)
    clock.heartbeat(wid, elapsed_seconds=0)
    output = client.get(f"/api/worlds/{wid}/player/life").json()["pending_outputs"]
    assert len(output) == 1 and output[0]["quantity"] == 3
    url = f"/api/worlds/{wid}/player/life/outputs/{output[0]['id']}/claim"
    with database.write() as c:
        c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (player,))
    assert client.post(url, json={"quantity": 3}).status_code == 409
    with database.write() as c:
        c.execute("UPDATE characters SET inventory_capacity=20 WHERE id=?", (player,))
    assert client.post(url, json={"quantity": 3}).status_code == 200
    assert client.post(url, json={"quantity": 3}).status_code == 404
    with database.read() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM item_instances WHERE container_type='ac"
                "tivity_escrow' AND container_id=?",
                (aid,),
            ).fetchone()[0]
            == 0
        )


def test_cancelled_repair_returns_target_unchanged_and_original_materials(life_world):
    client, database, wid, _, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    result = client.post(
        f"/api/worlds/{wid}/player/life/tasks",
        json={"recipe_id": recipe, "target_item_id": "repair-target"},
    )
    assert result.status_code == 201, result.text
    aid = result.json()["activity_id"]
    clock.heartbeat(wid, elapsed_seconds=600)
    client.post(f"/api/worlds/{wid}/player/life/activities/{aid}/stop")
    outputs = client.get(f"/api/worlds/{wid}/player/life").json()["pending_outputs"]
    assert sorted(item["quantity"] for item in outputs) == [1, 2]
    with database.read() as c:
        assert (
            c.execute("SELECT condition FROM item_instances WHERE id='repair-target'").fetchone()[0]
            == 50
        )


def test_completed_repair_changes_only_the_reserved_target(life_world, monkeypatch):
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: 1)
    client, database, wid, player, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    result = client.post(f"/api/worlds/{wid}/player/life/tasks", json={
        "recipe_id": recipe, "target_item_id": "repair-target",
    })
    assert result.status_code == 201, result.text
    clock.heartbeat(wid, elapsed_seconds=1800)
    clock.heartbeat(wid, elapsed_seconds=1800)
    with database.read() as c:
        target = c.execute("SELECT * FROM item_instances WHERE id='repair-target'").fetchone()
        assert target["condition"] == 70
        assert target["owner_character_id"] == player
        assert target["container_type"] == "activity_output"
        quantity = c.execute(
            "SELECT sum(quantity) FROM item_instances WHERE item_type_id='task-material'"
        ).fetchone()[0]
        assert quantity == 3


def test_missing_material_does_not_leave_active_task_or_deduct_stock(life_world):
    client, database, wid, player, _, _ = life_world
    recipe = register_recipe(life_world)
    with database.write() as c:
        c.execute("UPDATE item_instances SET quantity=1 WHERE id='material'")
    result = client.post(f"/api/worlds/{wid}/player/life/tasks", json={"recipe_id": recipe})
    assert result.status_code == 409
    with database.read() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM character_life_activities WHERE character_id=?", (player,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id='material'").fetchone()[0] == 1
        )


def test_advance_unknown_world_returns_not_found(life_world):
    client = life_world[0]
    response = client.post(
        "/api/worlds/missing-world/player/life/activities/missing-activity/advance",
        json={"expected_version": 0, "request_id": "missing-world-check"},
    )
    assert response.status_code == 404


def test_injury_before_waiting_does_not_skip_more_world_time(life_world):
    client, database, wid, player, _, _ = life_world
    aid = start_work(life_world)
    body = wait_request(life_world, aid)
    with database.write() as c:
        c.execute("UPDATE characters SET health=health-1 WHERE id=?", (player,))
        before = c.execute('SELECT w.current_time FROM worlds w WHERE id=?', (wid,)).fetchone()[0]
    result = client.post(f"/api/worlds/{wid}/player/life/activities/{aid}/advance", json=body)
    assert result.status_code == 409
    with database.read() as c:
        after = c.execute('SELECT w.current_time FROM worlds w WHERE id=?', (wid,)).fetchone()[0]
        assert before == after
