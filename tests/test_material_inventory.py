# ruff: noqa: F811
"""哥哥，沿用真实背包、室内和制作服务，检查污染不能绕过接口。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_interiors import enter, house, layout  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_production import setup_production

from world_engine.activity_tasks import TaskService
from world_engine.economy import EconomyService
from world_engine.food import FoodService
from world_engine.food_supply import seed_food_supply
from world_engine.interiors import InteriorService
from world_engine.inventory import InventoryService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService as M
from world_engine.repository import WorldRepository


def grain(c, wid, owner, quantity=10, *, type_id=None):
    tid, iid = type_id or str(uuid4()), str(uuid4())
    if not type_id:
        c.execute(
            (
                "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,"
                "?,'food',100,1)"
            ),
            (tid, "批次谷物" + tid),
        )
    c.execute(
        (
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
            "container_id,owner_character_id,quantity,condition) VALUES (?,?,?,"
            "'character_inventory',?,?,?,100)"
        ),
        (iid, wid, tid, owner, owner, quantity),
    )
    M.attach(c, iid, MaterialState(mass=quantity, mobile=quantity * 0.2, nutrition=quantity * 20))
    return iid, tid


def row(c, iid):
    return c.execute(
        (
            "SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN "
            "item_types t ON t.id=i.item_type_id WHERE i.id=?"
        ),
        (iid,),
    ).fetchone()


def test_transfer_split_then_merge_preserves_salt(house):
    _, db, wid, player, npc, _ = house
    with db.write() as c:
        iid, _ = grain(c, wid, player["id"])
        InventoryService.transfer(c, row(c, iid), npc["id"], 3)
        target = c.execute(
            "SELECT * FROM item_instances WHERE owner_character_id=? AND item_type_id=?",
            (npc["id"], row(c, iid)["item_type_id"]),
        ).fetchone()
        assert target["quantity"] == 3
        assert M.state(c, target["id"]).mobile == pytest.approx(0.6)
        InventoryService.transfer(c, row(c, iid), npc["id"], 2)
        target = c.execute("SELECT * FROM item_instances WHERE id=?", (target["id"],)).fetchone()
        assert target["quantity"] == 5
        assert M.state(c, target["id"]).mobile == pytest.approx(1)
        assert M.state(c, iid).mobile == pytest.approx(1)
        assert M.audit(c, wid)["balanced"]


def test_clean_untracked_stack_does_not_absorb_tracked_batch(house):
    _, db, wid, player, npc, _ = house
    with db.write() as c:
        iid, tid = grain(c, wid, player["id"])
        clean = str(uuid4())
        c.execute(
            (
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                "container_id,owner_character_id,quantity,condition) VALUES (?,?,?,"
                "'character_inventory',?,?,5,100)"
            ),
            (clean, wid, tid, npc["id"], npc["id"]),
        )
        InventoryService.transfer(c, row(c, iid), npc["id"], 3)
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (clean,)).fetchone()[0] == 5
        )
        assert M.state(c, iid).mobile == pytest.approx(1.4)
        assert M.audit(c, wid)["balanced"]


def test_fixture_storage_split_merge_and_withdraw_keep_state(house):
    _, db, wid, player, _, _ = house
    room, _, _ = layout(house, capacity=10)
    enter(house, room)
    with db.write() as c:
        fixture = c.execute(
            "SELECT * FROM life_fixtures WHERE room_id=? AND kind='container'", (room,)
        ).fetchone()
        c.execute("UPDATE life_fixtures SET is_open=1 WHERE id=?", (fixture["id"],))
        iid, _ = grain(c, wid, player["id"])
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        actor = c.execute("SELECT * FROM characters WHERE id=?", (player["id"],)).fetchone()
        InteriorService.transfer(c, actor, fixture["id"], iid, 3, True, at)
        InteriorService.transfer(c, actor, fixture["id"], iid, 2, True, at)
        stored = c.execute(
            (
                "SELECT * FROM item_instances WHERE container_type='fixture_storage' AND "
                "container_id=?"
            ),
            (fixture["id"],),
        ).fetchone()
        assert stored["quantity"] == 5
        assert M.state(c, stored["id"]).mobile == pytest.approx(1)
        InteriorService.transfer(c, actor, fixture["id"], stored["id"], 2, False, at)
        assert M.state(c, stored["id"]).mobile == pytest.approx(0.6)
        assert M.state(c, iid).mobile == pytest.approx(1.4)
        assert M.audit(c, wid)["balanced"]


def test_escrow_reservation_is_material_transfer_not_consumption(house):
    _, db, wid, player, _, _ = house
    with db.write() as c:
        iid, _ = grain(c, wid, player["id"])
        actor = c.execute("SELECT * FROM characters WHERE id=?", (player["id"],)).fetchone()
        reserved = TaskService._reserve(c, actor, row(c, iid), 3, "escrow-test")
        assert M.state(c, reserved).mobile == pytest.approx(0.6)
        assert M.state(c, iid).mobile == pytest.approx(1.4)
        assert M.audit(c, wid)["sinks"]["salt"] == 0
        assert M.audit(c, wid)["balanced"]


def test_eating_preserves_pollution_in_body_sink(house):
    _, db, wid, player, _, _ = house
    with db.write() as c:
        iid, _ = grain(c, wid, player["id"])
        actor = c.execute("SELECT * FROM characters WHERE id=?", (player["id"],)).fetchone()
        item = dict(row(c, iid))
        item["nutrition"] = 20
        FoodService.eat(c, actor, item, WorldRepository().get_snapshot(c, wid).world.current_time)
        assert M.state(c, iid).mobile == pytest.approx(1.8)
        sink = c.execute(
            "SELECT destination,state_json FROM material_sinks WHERE source_item_id=?", (iid,)
        ).fetchone()
        assert sink["destination"] == "body:" + player["id"]
        assert M.audit(c, wid)["sinks"]["salt"] == pytest.approx(0.2)
        assert M.audit(c, wid)["balanced"]


def test_generic_craft_cannot_turn_tracked_input_into_clean_output(town):
    e = town
    material, _, recipe_id, _ = setup_production(e, resource=False)
    with e.db.write() as c:
        iid = str(uuid4())
        c.execute(
            (
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                "container_id,owner_character_id,quantity,condition) VALUES (?,?,?,"
                "'character_inventory',?,?,2,100)"
            ),
            (iid, e.wid, material, e.npc, e.npc),
        )
        M.attach(c, iid, MaterialState(mass=2, mobile=0.2))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        with pytest.raises(ValueError, match="材料|追踪"):
            TaskService.prepare(c, actor, e.at, recipe_id=recipe_id)
        assert M.state(c, iid).mobile == 0.2


def test_late_pollution_cannot_bypass_craft_completion(town):
    e = town
    material, _, recipe_id, _ = setup_production(e, resource=False)
    with e.db.write() as c:
        iid = str(uuid4())
        c.execute(
            (
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                "container_id,owner_character_id,quantity,condition) VALUES (?,?,?,"
                "'character_inventory',?,?,2,100)"
            ),
            (iid, e.wid, material, e.npc, e.npc),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        activity_id, _, _ = TaskService.start(c, actor, e.at, recipe_id=recipe_id)
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE id=?", (activity_id,)
        ).fetchone()
        detail = c.execute(
            "SELECT spec_json FROM activity_task_details WHERE activity_id=?", (activity_id,)
        ).fetchone()
        reserved_id = json.loads(detail[0])["reserved_items"][0]
        M.attach(c, reserved_id, MaterialState(mass=2, mobile=0.2))
    # 哥哥，使用真实心跳记录，身体更新的外键也一起验证。
    e.clock.heartbeat(e.wid, elapsed_seconds=600)
    with e.db.write() as c:
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE id=?", (activity_id,)
        ).fetchone()
        with pytest.raises(ValueError, match="材料|追踪"):
            TaskService.finish(c, activity, "completed", None, e.at + timedelta(minutes=30))
        assert M.state(c, reserved_id).mobile == 0.2


def test_material_mode_blocks_automatic_food_growth_and_reboot_backfill(house):
    _, db, wid, _, _, loc = house
    with db.write() as c:
        c.execute("INSERT INTO material_modes(world_id) VALUES (?)", (wid,))
        c.execute(
            (
                "UPDATE world_item_profiles SET daily_growth=100,"
                "last_growth_world_time='2000-01-01T00:00:00+00:00' WHERE world_id=?"
            ),
            (wid,),
        )
        c.execute("UPDATE locations SET resources_json=? WHERE world_id=?", ('{"food":0}', wid))
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        EconomyService.tick(c, wid, at)
        assert (
            c.execute("SELECT resources_json FROM locations WHERE id=?", (loc["id"],)).fetchone()[0]
            == '{"food":0}'
        )
        c.execute("DELETE FROM world_item_profiles WHERE world_id=?", (wid,))
        seed_food_supply(c, wid)
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 0
        )
    db.initialize()
    with db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 0
        )


def test_material_mode_rejects_untracked_generic_harvest(house):
    _, db, wid, player, _, loc = house
    with db.write() as c:
        c.execute("INSERT INTO material_modes(world_id) VALUES (?)", (wid,))
        c.execute("UPDATE locations SET resources_json=? WHERE id=?", ('{"food":10}', loc["id"]))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (player["id"],)).fetchone()
        at = WorldRepository().get_snapshot(c, wid).world.current_time
        assert EconomyService.harvest(c, actor, at, food_only=True) is False
        assert (
            c.execute("SELECT resources_json FROM locations WHERE id=?", (loc["id"],)).fetchone()[0]
            == '{"food":10}'
        )
