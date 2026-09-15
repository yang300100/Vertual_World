# ruff: noqa: F811
"""食物期限跨库存操作保持，变质后不允许自动获益。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import definition, lodging, post, town  # noqa: F401

from scripts.prepare_life_content import export_packet, validate_draft
from world_engine.action_checks import CheckService
from world_engine.economy import EconomyService
from world_engine.food import FoodService
from world_engine.interiors import InteriorService
from world_engine.inventory import InventoryService
from world_engine.repository import to_iso


def food_type(e):
    return definition(
        e,
        {
            "element_type": "commodity",
            "name": "验收鲜果",
            "category": "food",
            "nutrition": 20,
            "shelf_life_hours": 1,
            "stack_limit": 10,
            "resource_location_id": e.loc["id"],
            "resource_key": "fresh_fruit",
            "initial_resource": 20,
        },
    )


def actor(c, cid):
    return c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()


def item(c, iid):
    return c.execute(
        "SELECT i.*,t.stack_limit,t.slot_size,t.name,p.nutrition FROM item_instances i "
        "JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id WHERE i.id=?",
        (iid,),
    ).fetchone()


def batch(c, e, tid, *, owner=None, born=None, quantity=4):
    iid = str(uuid4())
    cid = owner or e.player
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) VALUES (?,?,?,'character_inventory',?,?,100,?)",
        (iid, e.wid, tid, cid, quantity, cid),
    )
    FoodService.stamp(c, iid, tid, born or e.at)
    return iid


def set_time(c, e, at):
    c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))


@pytest.mark.parametrize(
    "minutes,state,nutrition",
    [(0, "fresh", 20), (44, "fresh", 20), (45, "stale", 10), (59, "stale", 10)],
)
def test_world_time_drives_freshness_and_nutrition(town, minutes, state, nutrition):
    e = town
    tid = food_type(e)
    with e.db.write() as c:
        iid = batch(c, e, tid)
        c.execute("UPDATE characters SET satiety=0 WHERE id=?", (e.player,))
        set_time(c, e, e.at + timedelta(minutes=minutes))
    result = post(e, f"player/life/items/{iid}/eat", {"request_id": str(uuid4())})
    assert result["effect"]["freshness"] == state
    assert result["effect"]["nutrition"] == nutrition
    with e.db.read() as c:
        assert actor(c, e.player)["satiety"] == nutrition


def test_split_trade_and_different_batches_do_not_refresh_or_merge(town):
    e = town
    tid = food_type(e)
    with e.db.write() as c:
        iid = batch(c, e, tid)
        later = batch(c, e, tid, owner=e.npc, born=e.at + timedelta(minutes=10))
        original = item(c, iid)
        InventoryService.transfer(c, original, e.npc, 2)
        rows = c.execute(
            "SELECT * FROM item_instances WHERE item_type_id=? AND container_id=?", (tid, e.npc)
        ).fetchall()
        assert len(rows) == 2
        moved = next(row for row in rows if row["id"] != later)
        assert moved["spoils_world_time"] == original["spoils_world_time"]
        InventoryService.transfer(c, item(c, moved["id"]), e.player, 1)
        assert item(c, iid)["quantity"] == 3
        assert item(c, iid)["spoils_world_time"] == to_iso(e.at + timedelta(hours=1))
    e.db.initialize()
    with e.db.read() as c:
        assert item(c, iid)["spoils_world_time"] == to_iso(e.at + timedelta(hours=1))


def test_cupboard_split_preserves_expiry_and_food_rots_in_storage(town):
    e = town
    tid = food_type(e)
    room = lodging(e)
    with e.db.write() as c:
        iid = batch(c, e, tid)
        p = actor(c, e.player)
        InteriorService.door(c, p, room, "open", e.at)
        InteriorService.door(c, p, room, "enter", e.at)
        p = actor(c, e.player)
        cabinet = c.execute(
            "SELECT id FROM life_fixtures WHERE room_id=? AND kind='container'", (room,)
        ).fetchone()[0]
        InteriorService.operate_fixture(c, p, cabinet, "open", e.at)
        InteriorService.transfer(c, p, cabinet, iid, 2, True, e.at)
        stored = c.execute(
            "SELECT id FROM item_instances WHERE container_id=?", (cabinet,)
        ).fetchone()[0]
        assert item(c, stored)["spoils_world_time"] == item(c, iid)["spoils_world_time"]
        set_time(c, e, e.at + timedelta(hours=1))
        assert FoodService.spoiled(item(c, stored), e.at + timedelta(hours=1))
        InteriorService.transfer(c, p, cabinet, stored, 1, False, e.at + timedelta(hours=1))
        assert item(c, iid)["quantity"] == 3
        assert FoodService.spoiled(item(c, iid), e.at + timedelta(hours=1))


def test_spoiled_food_is_not_auto_eaten_or_sold_and_explicit_risk_is_idempotent(town):
    e = town
    tid = food_type(e)
    with e.db.write() as c:
        iid = batch(c, e, tid, born=e.at - timedelta(hours=1))
        seller_item = batch(c, e, tid, owner=e.npc, born=e.at - timedelta(hours=1))
        c.execute(
            "DELETE FROM item_instances WHERE container_id=? AND item_type_id!=?", (e.player, tid)
        )
        assert EconomyService.food(c, actor(c, e.player), at=e.at) is None
        assert (
            EconomyService.buy_supply(c, actor(c, e.player), e.at, item_type_id=tid, budget=50)
            is None
        )
        before = actor(c, e.player)["energy"]
    r = e.client.post(
        f"/api/worlds/{e.wid}/player/life/items/{iid}/eat", json={"request_id": str(uuid4())}
    )
    assert r.status_code == 409
    r = e.client.post(
        f"/api/worlds/{e.wid}/player/trade",
        json={
            "request_id": str(uuid4()),
            "item_id": seller_item,
            "seller_id": e.npc,
            "quantity": 1,
            "operation": "buy",
            "expected_total": 3,
        },
    )
    assert r.status_code == 409
    body = {"request_id": str(uuid4()), "accept_spoiled": True}
    result = post(e, f"player/life/items/{iid}/eat", body)
    assert post(e, f"player/life/items/{iid}/eat", body) == result
    e.db.initialize()
    with e.db.write() as c:
        assert actor(c, e.player)["energy"] == before - 8
        assert item(c, iid)["quantity"] == 3
        assert FoodService.discomfort(c, e.player, e.at)["check_modifier"] == -10
        plan = CheckService.plan(
            c,
            actor(c, e.player),
            "repair",
            {"item_id": "dummy"},
            skill_name="修理",
            difficulty=40,
            method={},
        )
        assert plan["snapshot"]["modifiers"]["food_discomfort"] == -10
        set_time(c, e, e.at + timedelta(hours=6))
        assert FoodService.discomfort(c, e.player, e.at + timedelta(hours=6)) is None
        normal = CheckService.plan(
            c,
            actor(c, e.player),
            "repair",
            {"item_id": "dummy"},
            skill_name="修理",
            difficulty=40,
            method={},
        )
        assert normal["snapshot"]["modifier"] == plan["snapshot"]["modifier"] + 10


def recipe(e, ingredient, output):
    pending = post(
        e,
        "activity-recipes",
        {
            "idempotency_key": str(uuid4()),
            "recipe": {
                "name": "食材批次验收配方",
                "kind": "craft",
                "location_id": e.loc["id"],
                "duration_minutes": 30,
                "ingredients": [{"item_type_id": ingredient, "quantity": 1}],
                "output_item_type_id": output,
            },
        },
        201,
    )
    result = post(e, f"registrations/{pending['registration']['id']}/confirm")
    assert result["status"] == "applied"
    return result["result_entity_id"]


def test_perishable_materials_interrupt_at_expiry_without_refreshing_output(town):
    e = town
    tid = food_type(e)
    rid = recipe(e, tid, e.food)
    with e.db.write() as c:
        iid = batch(c, e, tid, born=e.at - timedelta(minutes=45))
    start = post(e, "player/life/tasks", {"recipe_id": rid}, 201)
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    with e.db.read() as c:
        task = c.execute(
            "SELECT * FROM character_life_activities WHERE id=?", (start["activity_id"],)
        ).fetchone()
        assert task["status"] == "interrupted"
        assert task["finished_world_time"] == to_iso(e.at + timedelta(minutes=15))
        assert (
            c.execute(
                "SELECT count(*) FROM item_instances WHERE container_id=? AND item_type_id=?",
                (task["id"], e.food),
            ).fetchone()[0]
            == 0
        )
        assert item(c, iid)["spoils_world_time"] == to_iso(e.at + timedelta(minutes=15))


def test_crafted_food_ages_from_completion_while_waiting_to_be_claimed(town):
    e = town
    tid = food_type(e)
    rid = recipe(e, e.food, tid)
    with e.db.write() as c:
        batch(c, e, e.food)
    start = post(e, "player/life/tasks", {"recipe_id": rid}, 201)
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    with e.db.write() as c:
        produced = c.execute(
            "SELECT * FROM item_instances WHERE container_id=? AND item_type_id=?",
            (start["activity_id"], tid),
        ).fetchone()
        assert produced["spoils_world_time"] == to_iso(e.at + timedelta(minutes=90))
        EconomyService.collect_outputs(c, actor(c, e.player), e.at + timedelta(minutes=100))
        result = item(c, produced["id"])
        assert FoodService.spoiled(result, e.at + timedelta(minutes=100))


def test_new_harvest_is_stamped_and_legacy_food_remains_untracked(town, tmp_path):
    e = town
    tid = food_type(e)
    with e.db.write() as c:
        assert EconomyService.harvest(c, actor(c, e.player), e.at, item_type_ids={tid})
        fresh = c.execute("SELECT * FROM item_instances WHERE item_type_id=?", (tid,)).fetchone()
        assert fresh["spoils_world_time"] == to_iso(e.at + timedelta(hours=1))
        legacy = c.execute(
            "SELECT * FROM item_instances WHERE item_type_id=? LIMIT 1", (e.food,)
        ).fetchone()
        assert FoodService.view(legacy, e.at)["state"] == "untracked"
    packet = export_packet(e.settings.database_path, e.wid, [e.loc["id"]], tmp_path / "packet")
    assert (
        next(p for p in packet["commodity_profiles"] if p["item_type_id"] == tid)[
            "shelf_life_hours"
        ]
        == 1
    )
    bad = {
        "format_version": 1,
        "world_id": e.wid,
        "entries": [
            {
                "key": "invalid",
                "source_note": "验证限制",
                "payload": {
                    "element_type": "commodity",
                    "name": "工具不能按食物腐败",
                    "category": "tool",
                    "shelf_life_hours": 2,
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="食物"):
        validate_draft(bad, packet)
