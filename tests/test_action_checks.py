# ruff: noqa: F811
"""检定的概率、冻结、幂等、库存后果与地图证据隔离。"""

import json
from uuid import uuid4

import pytest
from test_activity_tasks import register_recipe
from test_life import life_world  # noqa: F401

from world_engine.action_checks import CheckService, classify
from world_engine.repository import to_iso, utc_now


def checks(env):
    return env[0].get(f"/api/worlds/{env[2]}/player/life/checks").json()


def test_known_skill_practice_preserves_initial_proficiency(life_world, monkeypatch):
    client, database, wid, player, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    with database.write() as c:
        c.execute("UPDATE characters SET skills_json='[\"修理\"]' WHERE id=?", (player,))
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: 1)
    response = client.post(f"/api/worlds/{wid}/player/life/tasks", json={"recipe_id":recipe,"target_item_id":"repair-target"})
    assert response.status_code == 201, response.text
    clock.heartbeat(wid, elapsed_seconds=1800)
    database.initialize()
    clock.heartbeat(wid, elapsed_seconds=0)
    with database.read() as c:
        assert c.execute("SELECT proficiency FROM character_skill_proficiencies WHERE character_id=? AND skill_name='修理'",(player,)).fetchone()[0] == 12
        assert c.execute("SELECT sum(amount) FROM skill_practice_awards WHERE character_id=?",(player,)).fetchone()[0] == 2


def test_learning_during_attempt_does_not_retroactively_award_practice(life_world, monkeypatch):
    client, database, wid, player, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: 1)
    response = client.post(f"/api/worlds/{wid}/player/life/tasks", json={"recipe_id":recipe,"target_item_id":"repair-target"})
    assert response.status_code == 201, response.text
    with database.write() as c:
        c.execute("UPDATE characters SET skills_json='[\"修理\"]' WHERE id=?", (player,))
    clock.heartbeat(wid, elapsed_seconds=1800)
    with database.read() as c:
        assert c.execute("SELECT count(*) FROM skill_practice_awards WHERE character_id=?",(player,)).fetchone()[0] == 0


def claim_all(env):
    client, _, wid, _, _, _ = env
    for item in client.get(f"/api/worlds/{wid}/player/life").json()["pending_outputs"]:
        response = client.post(f"/api/worlds/{wid}/player/life/outputs/{item['id']}/claim", json={"quantity": item["quantity"]})
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("roll,grade,condition,materials", [(1,"success",70,3),(20,"partial",60,3),(99,"failure",50,4)])
def test_repair_grades_freeze_and_have_bounded_cost(life_world, monkeypatch, roll, grade, condition, materials):
    client, database, wid, player, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    calls = []
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: calls.append(roll) or roll)
    payload = {"recipe_id":recipe, "target_item_id":"repair-target", "request_id":str(uuid4())}
    preview = client.post(f"/api/worlds/{wid}/player/life/tasks/preview", json=payload)
    assert preview.status_code == 200 and preview.json()["check"]["chance"] == 10
    assert calls == [] and checks(life_world) == []
    started = client.post(f"/api/worlds/{wid}/player/life/tasks", json=payload)
    assert started.status_code == 201, started.text
    pending = checks(life_world)[0]
    assert pending["roll"] is None and pending["outcome"] is None
    with database.write() as c:
        c.execute("INSERT INTO character_skill_proficiencies(character_id,world_id,skill_name,proficiency,updated_at) VALUES (?,?,'修理',100,?)",(player,wid,to_iso(utc_now())))
    database.initialize()
    clock.heartbeat(wid, elapsed_seconds=1800)
    result = checks(life_world)[0]
    assert result["chance"] == 10 and result["roll"] == roll and result["outcome"] == grade
    assert result["skill"] == 0 and calls == [roll]
    with database.read() as c:
        assert c.execute("SELECT condition FROM item_instances WHERE id='repair-target'").fetchone()[0] == condition
        assert c.execute("SELECT sum(quantity) FROM item_instances WHERE item_type_id='task-material'").fetchone()[0] == materials
    claim_all(life_world)
    replay = client.post(f"/api/worlds/{wid}/player/life/tasks", json=payload)
    assert replay.json() == started.json() and calls == [roll]


def test_cancel_then_retry_keeps_hidden_roll_and_resolved_failure_cannot_be_rerolled(life_world, monkeypatch):
    client, database, wid, _, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    rolls = []
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: rolls.append(99) or 99)
    payload = {"recipe_id":recipe,"target_item_id":"repair-target"}
    first = client.post(f"/api/worlds/{wid}/player/life/tasks", json=payload).json()["activity_id"]
    client.post(f"/api/worlds/{wid}/player/life/activities/{first}/stop")
    assert checks(life_world)[0]["roll"] is None
    claim_all(life_world)
    second = client.post(f"/api/worlds/{wid}/player/life/tasks", json=payload)
    assert second.status_code == 201 and rolls == [99]
    assert len(checks(life_world)) == 1
    clock.heartbeat(wid, elapsed_seconds=1800)
    claim_all(life_world)
    assert client.post(f"/api/worlds/{wid}/player/life/tasks", json=payload).status_code == 409
    assert rolls == [99]
    with database.read() as c:
        assert c.execute("SELECT count(*) FROM action_check_attempts").fetchone()[0] == 1


def test_missing_material_or_required_knowledge_never_rolls(life_world, monkeypatch):
    client, database, wid, _, _, _ = life_world
    recipe = register_recipe(life_world, "repair")
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: pytest.fail("前提不满足时不能掷骰"))
    payload = {"recipe_id":recipe,"target_item_id":"repair-target"}
    with database.write() as c:
        spec = json.loads(c.execute("SELECT spec_json FROM activity_recipes WHERE id=?",(recipe,)).fetchone()[0])
        spec["check_min_proficiency"] = 30
        c.execute("UPDATE activity_recipes SET spec_json=? WHERE id=?", (json.dumps(spec),recipe))
    assert client.post(f"/api/worlds/{wid}/player/life/tasks",json=payload).status_code == 409
    assert checks(life_world) == []


def test_skilled_routine_repair_succeeds_without_random_roll(life_world, monkeypatch):
    client, database, wid, player, _, clock = life_world
    recipe = register_recipe(life_world, "repair")
    with database.write() as c:
        c.execute("INSERT INTO character_skill_proficiencies(character_id,world_id,skill_name,proficiency,updated_at) VALUES (?,?,'修理',100,?)", (player,wid,to_iso(utc_now())))
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: pytest.fail("熟练者常规动作不掷骰"))
    result = client.post(f"/api/worlds/{wid}/player/life/tasks",json={"recipe_id":recipe,"target_item_id":"repair-target"})
    assert result.status_code == 201
    clock.heartbeat(wid,elapsed_seconds=1800)
    assert checks(life_world)[0]["automatic"]
    assert checks(life_world)[0]["outcome"] == "success" and checks(life_world)[0]["roll"] is None


def map_record(env):
    client,database,wid,player,_,_ = env
    with database.write() as c:
        c.execute("UPDATE characters SET energy=80,inventory_capacity=20 WHERE id=?", (player,))
        c.execute("INSERT INTO item_types(id,name,category) VALUES ('check-map','参考地图','map')")
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,quantity,condition,owner_character_id) VALUES ('map-ref',?,'check-map',?,'character_inventory',1,100,?)", (wid,player,player))
    action = client.post(f"/api/worlds/{wid}/player/act",json={"intent":"动作：记录道路"})
    assert action.status_code == 201, action.text
    records = client.get(f"/api/worlds/{wid}/player/activities").json()
    return next(item["id"] for item in records if item["step_key"] == "road_notes")


@pytest.mark.parametrize("roll,grade", [(1,"success"),(30,"partial"),(99,"failure")])
def test_map_review_never_changes_original_observations_or_world_terrain(life_world,monkeypatch,roll,grade):
    client,database,wid,_,_,clock=life_world
    record = map_record(life_world)
    with database.read() as c:
        original = c.execute("SELECT content_json FROM player_activity_records WHERE id=?",(record,)).fetchone()[0]
        terrain = [tuple(row) for row in c.execute("SELECT id,longitude,latitude FROM locations ORDER BY id")]
    monkeypatch.setattr("world_engine.action_checks.draw",lambda:roll)
    response=client.post(f"/api/worlds/{wid}/player/life/tasks",json={"map_record_id":record})
    assert response.status_code==201,response.text
    clock.heartbeat(wid,elapsed_seconds=1800)
    assert checks(life_world)[0]["outcome"]==grade
    with database.read() as c:
        assert original==c.execute("SELECT content_json FROM player_activity_records WHERE id=?",(record,)).fetchone()[0]
        assert terrain==[tuple(row) for row in c.execute("SELECT id,longitude,latitude FROM locations ORDER BY id")]
        review=c.execute("SELECT * FROM player_activity_records WHERE step_key='map_review'").fetchone()
        assert review["status"]==("completed" if grade=="success" else "partial")
        assert json.loads(review["content_json"])["source_record_id"]==record
    assert client.post(f"/api/worlds/{wid}/player/life/tasks",json={"map_record_id":record}).status_code==409


def test_probability_monotonicity_and_percentile_outcomes(life_world):
    _,database,wid,player,_,_=life_world
    chances=[]
    with database.write() as c:
        for skill in (0,20,40,60,80,100):
            c.execute("INSERT INTO character_skill_proficiencies(character_id,world_id,skill_name,proficiency,updated_at) VALUES (?,?,'修理',?,?) ON CONFLICT(character_id,skill_name) DO UPDATE SET proficiency=excluded.proficiency",(player,wid,skill,to_iso(utc_now())))
            actor=c.execute("SELECT * FROM characters WHERE id=?",(player,)).fetchone()
            chances.append(CheckService.plan(c,actor,"repair",{},skill_name="修理",difficulty=50,method={})["snapshot"]["chance"])
    assert chances==sorted(chances)
    outcomes=[classify(40,roll) for roll in range(1,101)]
    assert outcomes.count("success")==40 and outcomes.count("partial")==20 and outcomes.count("failure")==40
