# ruff: noqa: F811
"""隔离世界验证备料、真实采购、工具磨损与中止重放。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import definition, post, town  # noqa: F401
from test_routines import register, spec

from scripts.prepare_life_content import export_packet, validate_draft
from world_engine.activity_tasks import TaskService
from world_engine.economy import EconomyService
from world_engine.inventory import InventoryService
from world_engine.life import LifeActivityService
from world_engine.routines import RoutineService


def setup_production(e, *, resource=True, budget=0, quantity=2, wear=4):
    material = definition(
        e,
        {
            "element_type": "commodity",
            "name": "隔离木料",
            "category": "material",
            "price": 4,
            **(
                {
                    "resource_location_id": e.loc["id"],
                    "resource_key": "test_wood",
                    "initial_resource": 20,
                }
                if resource
                else {}
            ),
        },
    )
    tool = definition(e, {"element_type": "commodity", "name": "隔离小锯", "category": "tool"})
    recipe = {
        "name": "隔离木工",
        "kind": "craft",
        "location_id": e.loc["id"],
        "duration_minutes": 30,
        "energy_cost": 10,
        "ingredients": [{"item_type_id": material, "quantity": quantity}],
        "tools": [{"item_type_id": tool, "wear": wear}],
        "output_item_type_id": e.food,
    }
    pending = post(e, "activity-recipes", {"idempotency_key": str(uuid4()), "recipe": recipe}, 201)
    applied = post(e, f"registrations/{pending['registration']['id']}/confirm")
    assert applied["status"] == "applied"
    recipe_id = applied["result_entity_id"]
    body = spec(e)
    body["slots"][0].update(
        kind="craft",
        recipe_id=recipe_id,
        duration_minutes=180,
        target_minutes=30,
        supply_purchase_budget=budget,
    )
    register(e, body)
    with e.db.write() as c:
        for cid in (e.npc, e.player):
            c.execute("UPDATE characters SET inventory_capacity=40,energy=90 WHERE id=?", (cid,))
            stock(c, e, tool, cid, 2, 20)
    return material, tool, recipe_id, recipe


def stock(c, e, item_type, cid, quantity, condition=100):
    iid = str(uuid4())
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
        "quantity,condition,owner_character_id) VALUES (?,?,?,'character_inventory',?,?,?,?)",
        (iid, e.wid, item_type, cid, quantity, condition, cid),
    )
    return iid


def actor(c, cid):
    return c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()


def perform(e, minutes):
    with e.db.write() as c:
        npc = actor(c, e.npc)
        at = e.at + timedelta(minutes=minutes)
        return RoutineService.perform(c, npc, at, RoutineService.context(c, npc, at), None)


def test_harvest_only_shortage_then_complete_real_craft(town):
    e = town
    material, tool, _, _ = setup_production(e)
    perform(e, 5)
    e.db.initialize()
    perform(e, 5)
    perform(e, 19)
    with e.db.read() as c:
        assert c.execute("SELECT count(*) FROM npc_routine_supplies").fetchone()[0] == 1
        assert not LifeActivityService.running(c, e.npc)
        assert not TaskService.missing_supplies(
            c,
            actor(c, e.npc),
            {"kind": "craft", "ingredients": [{"item_type_id": material, "quantity": 1}]},
        )
    perform(e, 20)
    perform(e, 35)
    with e.db.write() as c:
        task = LifeActivityService.running(c, e.npc)
        assert task["kind"] == "craft"
        assert c.execute("SELECT count(*) FROM npc_routine_supplies").fetchone()[0] == 2
        assert (
            c.execute(
                "SELECT count(*) FROM npc_routine_supplies WHERE source_event_id IS NULL"
            ).fetchone()[0]
            == 0
        )
    e.clock.heartbeat(e.wid, elapsed_seconds=65 * 60)
    with e.db.write() as c:
        EconomyService.collect_outputs(c, actor(c, e.npc), e.at + timedelta(minutes=65))
        assert not LifeActivityService.running(c, e.npc)
        assert (
            c.execute(
                "SELECT sum(quantity) FROM item_instances WHERE item_type_id=? AND condition=16",
                (tool,),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT sum(quantity) FROM item_instances WHERE item_type_id=?", (material,)
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("minutes,wear_expected", [(0, 0), (1, 1), (15, 2), (30, 4)])
def test_tool_stack_reservation_and_interruption_are_conservative(town, minutes, wear_expected):
    e = town
    material, tool, recipe_id, _ = setup_production(e)
    with e.db.write() as c:
        stock(c, e, material, e.player, 2)
    request_id = str(uuid4())
    request = {"recipe_id": recipe_id, "request_id": request_id}
    start = post(e, "player/life/tasks", request, 201)
    assert post(e, "player/life/tasks", request, 201) == start
    with e.db.write() as c:
        task = LifeActivityService.running(c, e.player)
        detail = json.loads(
            c.execute(
                "SELECT spec_json FROM activity_task_details WHERE activity_id=?", (task["id"],)
            ).fetchone()[0]
        )
        assert len(detail["reserved_tools"]) == 1
        assert "reserved_tools" not in TaskService.view(c, task, e.at)["task_plan"]
        reserved_id = detail["reserved_tools"][0]["item_id"]
        assert (
            c.execute("SELECT quantity FROM item_instances WHERE id=?", (reserved_id,)).fetchone()[
                0
            ]
            == 1
        )
        LifeActivityService.interrupt(c, e.player, e.at + timedelta(minutes=minutes), "主动停止")
        before = InventoryService.snapshot(c, e.wid)
        LifeActivityService.interrupt(c, e.player, e.at + timedelta(minutes=minutes), "重复停止")
        assert before == InventoryService.snapshot(c, e.wid)
        reserved = c.execute("SELECT * FROM item_instances WHERE id=?", (reserved_id,)).fetchone()
        assert reserved["condition"] == 20 - wear_expected
        assert reserved["container_type"] == "activity_output"
        assert (
            c.execute(
                "SELECT sum(quantity) FROM item_instances WHERE item_type_id=?", (material,)
            ).fetchone()[0]
            == 2
        )
        c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (e.player,))
        c.execute(
            "UPDATE item_instances SET condition=100 WHERE item_type_id=? "
            "AND container_type='character_inventory' AND container_id=?",
            (tool, e.player),
        )
        EconomyService.collect_outputs(c, actor(c, e.player), e.at)
        assert (
            c.execute(
                "SELECT container_type FROM item_instances WHERE id=?", (reserved_id,)
            ).fetchone()[0]
            == "activity_output"
        )


def test_missing_or_broken_tool_does_not_reserve_materials(town):
    e = town
    material, tool, recipe_id, _ = setup_production(e)
    with e.db.write() as c:
        stock(c, e, material, e.player, 2)
        c.execute("UPDATE item_instances SET condition=3 WHERE item_type_id=?", (tool,))
        before = InventoryService.snapshot(c, e.wid)
    response = e.client.post(
        f"/api/worlds/{e.wid}/player/life/tasks", json={"recipe_id": recipe_id}
    )
    assert response.status_code == 409
    with e.db.read() as c:
        assert before == InventoryService.snapshot(c, e.wid)
        assert not LifeActivityService.running(c, e.player)


def seller_stock(e, material):
    with e.db.write() as c:
        seller = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.npc, e.player),
        ).fetchone()[0]
        c.execute(
            "UPDATE characters SET identity='木料商人',health=100,current_location_id=?,"
            "current_room_id=NULL,longitude=?,latitude=? WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], seller),
        )
        stock(c, e, material, seller, 8)
        return seller


@pytest.mark.parametrize(
    "budget,money,expected",
    [(0, 200, 0), (3, 200, 0), (4, 200, 1), (8, 200, 2), (8, 33, 0), (8, 34, 1)],
)
def test_purchase_budget_and_living_reserve_survive_restart(town, budget, money, expected):
    e = town
    material, _, _, _ = setup_production(e, resource=False, budget=budget, quantity=3)
    seller = seller_stock(e, material)
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=? WHERE id=?", (money, e.npc))
        seller_before = actor(c, seller)["money"]
    perform(e, 5)
    e.db.initialize()
    perform(e, 20)
    perform(e, 35)
    with e.db.read() as c:
        assert actor(c, e.npc)["money"] == money - expected * 4
        assert actor(c, seller)["money"] == seller_before + expected * 4
        assert c.execute("SELECT count(*) FROM npc_routine_supplies").fetchone()[0] == expected
        assert not LifeActivityService.running(c, e.npc)
        assert "recent_supplies" not in RoutineService.view(c, e.wid, e.npc, public_only=True)
        assert (
            "supply_purchase_budget"
            not in RoutineService.view(c, e.wid, e.npc, public_only=True)["slots"][0]
        )


@pytest.mark.parametrize(
    "obstacle", ["private_resource", "full_bag", "too_late", "other_owner", "distant_seller"]
)
def test_blocked_supply_keeps_inventory_and_money(town, obstacle):
    e = town
    material, _, _, _ = setup_production(e, resource=obstacle == "private_resource", budget=12)
    seller = seller_stock(e, material)
    with e.db.write() as c:
        if obstacle == "private_resource":
            c.execute(
                "UPDATE world_item_profiles SET resource_owner_id=? WHERE item_type_id=?",
                (e.player, material),
            )
            c.execute("UPDATE characters SET identity='旅人' WHERE id=?", (seller,))
        if obstacle == "full_bag":
            c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (e.npc,))
        if obstacle == "other_owner":
            c.execute(
                "UPDATE item_instances SET owner_character_id=? WHERE item_type_id=?",
                (e.player, material),
            )
        if obstacle == "distant_seller":
            c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (seller,))
        before = InventoryService.snapshot(c, e.wid)
        money = actor(c, e.npc)["money"]
    perform(e, 155 if obstacle == "too_late" else 5)
    with e.db.read() as c:
        assert before == InventoryService.snapshot(c, e.wid)
        assert actor(c, e.npc)["money"] == money
        assert not c.execute("SELECT 1 FROM npc_routine_supplies").fetchone()


def test_tool_draft_schema_and_registration_reject_conflicting_ingredients(town, tmp_path):
    e = town
    material, _, _, recipe = setup_production(e)
    recipe.update(
        element_type="activity_recipe",
        name="不合法工具配方",
        tools=[{"item_type_id": material, "wear": 2}],
    )
    export_packet(e.settings.database_path, e.wid, [e.loc["id"]], tmp_path / "packet")
    catalogue = json.loads((tmp_path / "packet" / "catalogue.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (tmp_path / "packet" / "schema-activity_recipe.json").read_text(encoding="utf-8")
    )
    assert "tools" in schema["properties"]
    with pytest.raises(ValueError, match="工具"):
        validate_draft(
            {
                "format_version": 1,
                "world_id": e.wid,
                "entries": [{"key": "bad-tool", "source_note": "隔离校验", "payload": recipe}],
            },
            catalogue,
        )
    pending = post(e, "activity-recipes", {"idempotency_key": str(uuid4()), "recipe": recipe}, 201)
    result = post(e, f"registrations/{pending['registration']['id']}/confirm")
    assert result["status"] == "rejected"


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_repair_wears_one_tool_and_preserves_target(town, monkeypatch, legacy):
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: 100)
    e = town
    material, tool, _, recipe = setup_production(e)
    recipe.update(
        name="隔离修理",
        kind="repair",
        output_item_type_id=None,
        repair_category="tool",
        check_tool_type_id=tool,
        tools=[] if legacy else [{"item_type_id": tool, "wear": 20}],
    )
    pending = post(e, "activity-recipes", {"idempotency_key": str(uuid4()), "recipe": recipe}, 201)
    applied = post(e, f"registrations/{pending['registration']['id']}/confirm")
    assert applied["status"] == "applied"
    with e.db.write() as c:
        stock(c, e, material, e.player, 2)
        target = stock(c, e, tool, e.player, 1, 50)
    post(
        e,
        "player/life/tasks",
        {"recipe_id": applied["result_entity_id"], "target_item_id": target},
        201,
    )
    with e.db.write() as c:
        task = LifeActivityService.running(c, e.player)
        detail = json.loads(
            c.execute(
                "SELECT spec_json FROM activity_task_details WHERE activity_id=?", (task["id"],)
            ).fetchone()[0]
        )
        assert len(detail["reserved_tools"]) == 1
    e.clock.heartbeat(e.wid, elapsed_seconds=30 * 60)
    with e.db.write() as c:
        result = json.loads(
            c.execute(
                "SELECT result_json FROM activity_task_details WHERE activity_id=?", (task["id"],)
            ).fetchone()[0]
        )
        assert result["check"]["outcome"] == "failure"
        assert result["tool_wear"][0]["condition"] == (19 if legacy else 0)
        assert (
            c.execute("SELECT condition FROM item_instances WHERE id=?", (target,)).fetchone()[0]
            == 50
        )
        before = InventoryService.snapshot(c, e.wid)
        assert TaskService.finish(c, task, "completed", "unused", e.at) == result
        assert before == InventoryService.snapshot(c, e.wid)
