"""哥哥，食用时必须核对当前实物，不能使用已转移的旧快照。"""

from datetime import timedelta
from uuid import uuid4

import pytest

from test_rides import current, ride_world  # noqa: F401
from world_engine.food import FoodService
from world_engine.inventory import InventoryError, InventoryService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import to_iso


def food_fixture(c, e, *, tracked=False):
    item_id = str(uuid4())
    c.execute("INSERT INTO item_types(id,name,category,stack_limit,usable,heal) VALUES (?,'隔离食物','food',10,1,0)", (item_id,))
    c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,owner_character_id,quantity,condition,spoils_world_time) "
              "VALUES (?,?,?,?,'character_inventory',?,2,100,?)",
              (item_id, e.wid, item_id, e.npc, e.npc, to_iso(current(e, c) + timedelta(days=1))))
    if tracked:
        MaterialService.attach(c, item_id, MaterialState(carrier="grain", mass=2, nutrition=40))
    actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
    item = c.execute("SELECT i.*,t.name,t.category,t.stack_limit,t.slot_size,30 AS nutrition "
                     "FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id=?", (item_id,)).fetchone()
    return actor, item


@pytest.mark.parametrize("tracked", [False, True])
def test_transferred_food_cannot_be_eaten_via_old_owner_snapshot(ride_world, tracked):
    e = ride_world
    with e.db.write() as c:
        actor, item = food_fixture(c, e, tracked=tracked)
        c.execute("UPDATE characters SET inventory_capacity=100 WHERE id=?", (e.player,))
        InventoryService.transfer(c, item, e.player, 2, preserve_identity=True)
        before = tuple(c.execute("SELECT owner_character_id,container_id,quantity FROM item_instances WHERE id=?", (item["id"],)).fetchone())
        satiety = c.execute("SELECT satiety FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        with pytest.raises(InventoryError, match="自己随身携带"):
            FoodService.eat(c, actor, item, current(e, c))
        assert tuple(c.execute("SELECT owner_character_id,container_id,quantity FROM item_instances WHERE id=?", (item["id"],)).fetchone()) == before
        assert c.execute("SELECT satiety FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == satiety
        if tracked:
            assert MaterialService.state(c, item["id"]).mass == 2


def test_stale_freshness_cannot_hide_current_spoilage(ride_world):
    e = ride_world
    with e.db.write() as c:
        actor, item = food_fixture(c, e)
        c.execute("UPDATE item_instances SET spoils_world_time=? WHERE id=?", (to_iso(current(e, c)), item["id"]))
        with pytest.raises(InventoryError, match="变质"):
            FoodService.eat(c, actor, item, current(e, c))
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (item["id"],)).fetchone()[0] == 2


def test_live_owner_eats_with_current_quantity_and_material(ride_world):
    e = ride_world
    with e.db.write() as c:
        actor, item = food_fixture(c, e, tracked=True)
        c.execute("UPDATE characters SET satiety=20 WHERE id=?", (e.npc,))
        result = FoodService.eat(c, actor, item, current(e, c))
        assert result["nutrition"] == 20
        assert c.execute("SELECT quantity FROM item_instances WHERE id=?", (item["id"],)).fetchone()[0] == 1
        assert c.execute("SELECT satiety FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 40
        assert MaterialService.state(c, item["id"]).mass == 1


def test_old_quantity_does_not_change_actual_material_accounting(ride_world):
    e = ride_world
    with e.db.write() as c:
        actor, item = food_fixture(c, e, tracked=True)
        MaterialService.consume(c, item, 1, "body:" + e.npc)
        c.execute("UPDATE characters SET satiety=20 WHERE id=?", (e.npc,))
        result = FoodService.eat(c, actor, item, current(e, c))
        assert result["nutrition"] == 20
        assert not c.execute("SELECT 1 FROM item_instances WHERE id=?", (item["id"],)).fetchone()
        assert c.execute("SELECT satiety FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 40


def test_registered_nutrition_is_read_from_current_world_profile(ride_world):
    e = ride_world
    with e.db.write() as c:
        source = c.execute("SELECT source_event_id FROM element_registration_requests WHERE element_type='vehicle' AND result_entity_id=?", (e.vehicle,)).fetchone()[0]
        registration = WorldElementRegistry().submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
            "source_event_id": source, "idempotency_key": "food-current-nutrition",
            "payload": {"element_type": "commodity", "name": "隔离登记食物", "category": "food", "nutrition": 23},
        }))
        assert registration.status == "applied"
        item_id = str(uuid4())
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,owner_character_id,quantity,condition) "
                  "VALUES (?,?,?,?,'character_inventory',?,2,100)",
                  (item_id, e.wid, registration.result_entity_id, e.npc, e.npc))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        item = dict(c.execute("SELECT * FROM item_instances WHERE id=?", (item_id,)).fetchone())
        item["nutrition"] = 999
        c.execute("UPDATE characters SET satiety=20 WHERE id=?", (e.npc,))
        result = FoodService.eat(c, actor, item, current(e, c))
        assert result["nutrition"] == 23
        assert c.execute("SELECT satiety FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 43
