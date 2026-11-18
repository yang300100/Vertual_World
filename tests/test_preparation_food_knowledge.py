"""哥哥，筹备和家庭照护只能使用本人真实知道的食物地点。"""

import json
from uuid import uuid4

import pytest

from test_lived_world import town  # noqa: F401
from test_families import declared_adult_player, household  # noqa: F401
from test_shared_plans import choose, offer
from world_engine.economy import EconomyService
from world_engine.families import FamilyService
from world_engine.shared_plans import SharedPlanService


def prepare(e, mode):
    if mode == "shared":
        plan = offer(e, own={"kind": "prepare_food", "target": 1, "spending_limit": 0},
                     other={"kind": "earn_money", "target": 1})
        assert choose(e, "accept", plan).status_code == 200
    else:
        household(e)
    remote = str(uuid4())
    with e.db.write() as c:
        # 哥哥，只布置隔离测试初始条件；之后选路和采集都经过现有服务。
        c.execute("DELETE FROM item_instances WHERE world_id=? AND container_type='character_inventory' AND container_id=?", (e.wid, e.npc))
        for row in c.execute("SELECT id,resources_json FROM locations WHERE world_id=?", (e.wid,)).fetchall():
            values = json.loads(row["resources_json"] or "{}")
            for key in ("food", "bakery_supply"):
                if key in values:
                    values[key] = 0
            c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps(values), row["id"]))
        c.execute("INSERT INTO locations(id,world_id,name,kind,resources_json,longitude,latitude,space_layer,is_active) VALUES (?,?,?,'place',?,?,?,'ground',1)",
                  (remote, e.wid, "隔离食物点", json.dumps({"food": 10}), e.loc["longitude"] + 0.005, e.loc["latitude"]))
    return remote


def perform(c, e, mode):
    actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
    places = c.execute("SELECT * FROM locations WHERE world_id=? AND is_active=1", (e.wid,)).fetchall()
    if mode == "shared":
        return SharedPlanService.perform(c, actor, e.at, places, None, None)
    return FamilyService.care_intention(c, actor, e.at, places, None)


@pytest.mark.parametrize("mode", ["shared", "family"])
def test_unknown_remote_food_never_directs_preparation_or_family_care(town, mode):
    e = town
    remote = prepare(e, mode)
    with e.db.write() as c:
        assert not c.execute("SELECT 1 FROM character_event_knowledge k JOIN world_events ev ON ev.id=k.event_id WHERE k.world_id=? AND k.character_id=? AND ev.location_id=?", (e.wid, e.npc, remote)).fetchone()
        before = tuple(c.execute("SELECT money,longitude,latitude FROM characters WHERE id=?", (e.npc,)).fetchone())
        perform(c, e, mode)
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving' AND destination_longitude=?", (e.npc, e.loc["longitude"] + 0.005)).fetchone()
        assert tuple(c.execute("SELECT money,longitude,latitude FROM characters WHERE id=?", (e.npc,)).fetchone()) == before
        assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (remote,)).fetchone()[0])["food"] == 10


@pytest.mark.parametrize("mode", ["shared", "family"])
def test_known_source_choice_does_not_peek_at_remote_current_stock(town, mode):
    e = town
    remote = prepare(e, mode)
    with e.db.write() as c:
        # 哥哥，这是隔离世界起点：本人在现场真正采集，之后再开始选路验证。
        c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=? WHERE id=?", (e.loc["longitude"] + 0.005, e.loc["latitude"], remote, remote, e.npc))
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        assert EconomyService.harvest(c, npc, e.at, food_only=True)
        c.execute("DELETE FROM item_instances WHERE world_id=? AND container_type='character_inventory' AND container_id=?", (e.wid, e.npc))
        c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=? WHERE id=?", (e.loc["longitude"], e.loc["latitude"], e.loc["id"], e.loc["id"], e.npc))
        results = []
        for stock in (0, 10):
            c.execute("SAVEPOINT remote_case")
            c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps({"food": stock}), remote))
            before = c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
            perform(c, e, mode)
            move = c.execute("SELECT destination_longitude,destination_latitude FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
            results.append(tuple(move) if move else None)
            assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == before
            assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (remote,)).fetchone()[0])["food"] == stock
            c.execute("ROLLBACK TO remote_case")
            c.execute("RELEASE remote_case")
        assert results == [(e.loc["longitude"] + 0.005, e.loc["latitude"])] * 2


@pytest.mark.parametrize("mode", ["shared", "family"])
def test_local_real_food_can_still_be_harvested(town, mode):
    e = town
    prepare(e, mode)
    with e.db.write() as c:
        c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps({"food": 5}), e.loc["id"]))
        before = c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        result = perform(c, e, mode)
        assert result and ("准备食物" in result or "真实食物" in result)
        assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc["id"],)).fetchone()[0])["food"] == 4
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == before
        assert c.execute("SELECT 1 FROM item_instances WHERE world_id=? AND owner_character_id=? AND container_type='character_inventory' AND container_id=?", (e.wid, e.npc, e.npc)).fetchone()
