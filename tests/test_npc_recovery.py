# ruff: noqa: F811
"""Yuki 验证 NPC 会依据真实采购阻碍调整下一步。"""

from test_lived_world import definition, town  # noqa: F401
from test_npc_goals import crafting, plan
from test_routines import register

from world_engine.daily_life import DailyLifeService
from world_engine.economy import EconomyService
from world_engine.life import LifeActivityService


def test_unaffordable_food_starts_real_work(town):
    e = town
    with e.db.write() as c:
        buyer = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=?,latitude=?,"
            "satiety=20,energy=90,money=3 WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], buyer),
        )
        c.execute("UPDATE world_item_profiles SET price=5 WHERE item_type_id=?", (e.food,))
        c.execute("UPDATE locations SET resources_json='{}' WHERE id=?", (e.loc["id"],))
        DailyLifeService.tick(c, e.wid, e.at)
        activity = LifeActivityService.running(c, buyer)
        assert activity is not None and activity["kind"] == "work"
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        assert c.execute("SELECT money FROM characters WHERE id=?", (buyer,)).fetchone()[0] >= 10
        assert c.execute(
            "SELECT COUNT(*) FROM world_events WHERE actor_id=? AND event_type='action.trade'",
            (buyer,),
        ).fetchone()[0] == 1


def test_purchase_obstacle_distinguishes_money_from_missing_stock(town):
    e = town
    with e.db.write() as c:
        buyer = c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()
        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=?,latitude=?,money=3 WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], buyer["id"]),
        )
        c.execute("UPDATE world_item_profiles SET price=5 WHERE item_type_id=?", (e.food,))
        buyer = c.execute("SELECT * FROM characters WHERE id=?", (buyer["id"],)).fetchone()
        obstacle = EconomyService.supply_obstacle(c, buyer, e.at, food_only=True)
        assert obstacle.kind == "insufficient_funds"
        assert obstacle.required_money == 5
        c.execute("DELETE FROM item_instances WHERE item_type_id=?", (e.food,))
        obstacle = EconomyService.supply_obstacle(c, buyer, e.at, food_only=True)
        assert obstacle.kind == "out_of_stock"


def test_craft_goal_does_not_work_for_goods_no_one_sells(town):
    e = town
    craft, _, _ = crafting(e, source=False, budget=4)
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=32 WHERE id=?", (e.npc,))
    register(e, plan(e, [craft]))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    with e.db.read() as c:
        activity = LifeActivityService.running(c, e.npc)
        state = c.execute(
            "SELECT status,reason FROM npc_life_goals WHERE character_id=?", (e.npc,)
        ).fetchone()
        assert activity is None
        assert state["status"] == "blocked"
        assert "卖家" in state["reason"] or "库存" in state["reason"]


def test_full_inventory_does_not_send_hungry_npc_to_work(town):
    e = town
    definition(e, {
        "element_type": "commodity",
        "name": "公共野果",
        "category": "food",
        "resource_location_id": e.loc["id"],
        "resource_key": "public_berries",
        "initial_resource": 10,
    })
    with e.db.write() as c:
        buyer = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute(
            "UPDATE characters SET current_location_id=?,longitude=?,latitude=?,"
            "satiety=20,energy=90,money=200,inventory_capacity=0 WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], buyer),
        )
        DailyLifeService.tick(c, e.wid, e.at)
        state = c.execute(
            "SELECT intention FROM npc_daily_states WHERE character_id=?", (buyer,)
        ).fetchone()
        assert LifeActivityService.running(c, buyer) is None
        assert "背包" in state["intention"]
