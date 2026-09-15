# ruff: noqa: F811
"""NPC生活目标需由真实状态满足，依赖、预算和修订不能伪造进展。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import definition, post, town  # noqa: F401
from test_routines import register

from scripts.prepare_life_content import export_packet, validate_draft
from world_engine.life import LifeActivityService
from world_engine.npc_goals import NpcGoalService
from world_engine.repository import to_iso
from world_engine.routines import RoutinePlanSpec
from world_engine.society import SocietyService


def plan(e, goals, **extra):
    return {
        "name": "有生活目标的作息",
        "character_id": e.npc,
        "slots": [
            {
                "key": "sleep",
                "name": "夜间休息",
                "kind": "rest",
                "starts_at": "22:00",
                "duration_minutes": 480,
                "location_id": e.loc["id"],
            }
        ],
        "ambitions": goals,
        **extra,
    }


def saving(e, **extra):
    return {
        "key": "saving",
        "title": "为生活留下一笔钱",
        "kind": "reserve_money",
        "target": 224,
        "work_location_ids": [e.loc["id"]],
        **extra,
    }


def goals(e):
    with e.db.read() as c:
        return NpcGoalService.view(c, e.npc)


def crafting(e, *, source=True, budget=0):
    material = definition(
        e,
        {
            "element_type": "commodity",
            "name": "目标用木料",
            "category": "material",
            "price": 4,
            **(
                {
                    "resource_location_id": e.loc["id"],
                    "resource_key": "goal_wood",
                    "initial_resource": 10,
                }
                if source
                else {}
            ),
        },
    )
    product = definition(e, {"element_type": "commodity", "name": "目标用木盒", "category": "tool"})
    proposed = post(
        e,
        "activity-recipes",
        {
            "idempotency_key": str(uuid4()),
            "recipe": {
                "name": "制作目标木盒",
                "kind": "craft",
                "location_id": e.loc["id"],
                "duration_minutes": 30,
                "energy_cost": 10,
                "ingredients": [{"item_type_id": material, "quantity": 1}],
                "output_item_type_id": product,
            },
        },
        201,
    )
    applied = post(e, f"registrations/{proposed['registration']['id']}/confirm")
    assert applied["status"] == "applied"
    return (
        {
            "key": "box",
            "title": "准备一只随身木盒",
            "kind": "craft_stock",
            "target": 1,
            "recipe_id": applied["result_entity_id"],
            "purchase_budget": budget,
            "work_location_ids": [e.loc["id"]],
        },
        material,
        product,
    )


def test_saving_uses_actual_wages_and_survives_restart(town):
    e = town
    register(e, plan(e, [saving(e)]))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert goals(e)[0]["status"] == "active"
    assert goals(e)[0]["progress"] == 200
    e.db.initialize()
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert goals(e)[0]["progress"] == 212
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert goals(e)[0]["status"] == "completed"
    e.clock.heartbeat(e.wid, elapsed_seconds=0)
    with e.db.read() as c:
        assert c.execute("SELECT count(*) FROM npc_goal_steps WHERE kind='work'").fetchone()[0] == 2
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 224


def test_dependency_unlocks_harvest_craft_and_actual_stock_completion(town):
    e = town
    craft, material, product = crafting(e)
    craft["depends_on"] = ["saving"]
    register(e, plan(e, [saving(e, target=212), craft]))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert next(g for g in goals(e) if g["goal"]["key"] == "box")["status"] == "waiting"
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert next(g for g in goals(e) if g["goal"]["key"] == "saving")["status"] == "completed"
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert all(g["status"] == "completed" for g in goals(e))
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT SUM(quantity) FROM item_instances WHERE item_type_id=? AND container_id=?",
                (product, e.npc),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute("SELECT count(*) FROM npc_goal_steps WHERE kind='harvest'").fetchone()[0] == 1
        )
        assert (
            c.execute("SELECT count(*) FROM npc_goal_steps WHERE kind='craft'").fetchone()[0] == 1
        )


def test_insufficient_spendable_money_leads_to_work_then_real_purchase(town):
    e = town
    craft, material, _ = crafting(e, source=False, budget=4)
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=32 WHERE id=?", (e.npc,))
        seller = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.npc, e.player),
        ).fetchone()[0]
        c.execute(
            "UPDATE characters SET identity='商人',current_location_id=?,longitude=?,latitude=? WHERE id=?",
            (e.loc["id"], e.loc["longitude"], e.loc["latitude"], seller),
        )
        c.execute(
            "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) VALUES (?,?,?,'character_inventory',?,5,100,?)",
            (str(uuid4()), e.wid, material, seller, seller),
        )
    register(e, plan(e, [craft]))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert goals(e)[0]["next_step"]["kind"] == "work"
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert goals(e)[0]["spent"] == 4
    e.db.initialize()
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert goals(e)[0]["status"] == "completed"
    assert goals(e)[0]["spent"] == 4


def test_blocked_high_priority_goal_does_not_starve_ready_goal(town):
    e = town
    craft, _, _ = crafting(e, source=False)
    craft["priority"] = 10
    register(e, plan(e, [craft, saving(e, target=212, priority=1)]))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert next(g for g in goals(e) if g["goal"]["key"] == "box")["status"] == "blocked"
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    assert next(g for g in goals(e) if g["goal"]["key"] == "saving")["next_step"]["kind"] == "work"


@pytest.mark.parametrize(
    "links",
    [[("a", ["a"])], [("a", ["missing"])], [("a", ["b"]), ("b", ["a"])], [("a", []), ("a", [])]],
)
def test_dependency_graph_rejects_invalid_references(town, links):
    with pytest.raises(ValueError):
        RoutinePlanSpec.model_validate(
            plan(town, [saving(town, key=key, depends_on=deps) for key, deps in links])
        )


def test_goal_deadline_and_fixed_schedule_prevent_overrun(town):
    e = town
    body = plan(e, [saving(e, deadline_world_time=to_iso(e.at + timedelta(minutes=30)))])
    register(e, body)
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    with e.db.read() as c:
        assert LifeActivityService.running(c, e.npc) is None
    e.clock.heartbeat(e.wid, elapsed_seconds=1800)
    assert goals(e)[0]["status"] == "expired"


def test_goal_respects_daily_labour_limit(town):
    e = town
    register(e, plan(e, [saving(e, target=400)], max_work_minutes_per_day=60))
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    assert goals(e)[0]["status"] == "blocked"
    with e.db.read() as c:
        assert c.execute("SELECT count(*) FROM npc_goal_steps WHERE kind='work'").fetchone()[0] == 1


def test_revision_preserves_started_activity_and_private_goal_history(town):
    e = town
    body = plan(e, [saving(e, title="只有自己知道的生活打算")])
    register(e, body)
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    original = goals(e)[0]["id"]
    with e.db.read() as c:
        aid = LifeActivityService.running(c, e.npc)["id"]
        context = SocietyService.personal_context(c, e.wid, e.npc, e.at + timedelta(minutes=5))
        assert context["life_goals"][0]["title"] == "只有自己知道的生活打算"
    public = e.client.get(f"/api/worlds/{e.wid}/player/experience").json()["public_routines"]
    assert "只有自己知道的生活打算" not in str(public)
    register(e, {**body, "expected_revision": 1, "enabled": False})
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM npc_life_goals WHERE id=?", (original,)).fetchone()[0]
            == "cancelled"
        )
        assert LifeActivityService.running(c, e.npc)["id"] == aid
    assert goals(e) == []
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        assert (
            c.execute("SELECT status FROM character_life_activities WHERE id=?", (aid,)).fetchone()[
                0
            ]
            == "completed"
        )


def test_content_draft_exports_goals_and_rejects_unknown_work(town, tmp_path):
    e = town
    register(e, plan(e, [saving(e)]))
    catalog = export_packet(e.settings.database_path, e.wid, [e.loc["id"]], tmp_path / "packet")
    assert catalog["npc_routines"][0]["spec"]["ambitions"]
    bad = plan(e, [saving(e, work_location_ids=["unknown"])], expected_revision=1)
    draft = {
        "format_version": 1,
        "world_id": e.wid,
        "entries": [
            {
                "key": "goal-plan",
                "source_note": "待审核目标",
                "payload": {"element_type": "npc_routine", **bad},
            }
        ],
    }
    with pytest.raises(ValueError, match="目标工作地点"):
        validate_draft(draft, catalog)


def test_failed_goal_travel_rolls_back_partial_changes(town, monkeypatch):
    e = town
    with e.db.write() as c:
        alternate = c.execute(
            "SELECT id FROM locations WHERE world_id=? AND id!=? LIMIT 1", (e.wid, e.loc["id"])
        ).fetchone()[0]
        c.execute(
            "UPDATE locations SET longitude=?,latitude=?,kind='workplace' WHERE id=?",
            (e.loc["longitude"] + 0.01, e.loc["latitude"], alternate),
        )
        before = c.execute("SELECT longitude FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
    register(e, plan(e, [saving(e, work_location_ids=[alternate])]))

    def rejected(c, actor, at, location):
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (actor["id"],))
        raise ValueError("测试路线条件已变化")

    monkeypatch.setattr("world_engine.daily_life.DailyLifeService._travel", rejected)
    e.clock.heartbeat(e.wid, elapsed_seconds=300)
    assert goals(e)[0]["status"] == "blocked"
    with e.db.read() as c:
        assert (
            c.execute("SELECT longitude FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
            == before
        )
        assert not c.execute("SELECT 1 FROM npc_goal_steps WHERE kind='travel'").fetchone()
