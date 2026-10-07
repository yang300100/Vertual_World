"""NPC 自主目标应当是可恢复的世界状态，而非一次决策文本。"""

# ruff: noqa: F811

from test_lived_world import town  # noqa: F401


def test_goal_adoption_is_idempotent_and_persistent(town):
    from world_engine.npc_agency import NpcAgencyService

    e = town
    predicate = {"kind": "money_at_least", "amount": 220}
    with e.db.write() as c:
        first = NpcAgencyService.adopt(
            c, e.wid, e.npc, "earn_money", predicate, "准备购买旅行用品", e.at,
        )
        second = NpcAgencyService.adopt(
            c, e.wid, e.npc, "earn_money", predicate, "准备购买旅行用品", e.at,
        )
        assert first == second
    with e.db.read() as c:
        goals = NpcAgencyService.view(c, e.npc)
        assert len([goal for goal in goals if goal["id"] == first]) == 1
        assert next(goal for goal in goals if goal["id"] == first)["motive"] == "准备购买旅行用品"
    e.db.initialize()
    with e.db.read() as c:
        assert any(goal["id"] == first for goal in NpcAgencyService.view(c, e.npc))


def test_actions_record_real_effects_and_causes(town):
    import json
    from uuid import uuid4

    from world_engine.actions import ActionService
    from world_engine.domain import ActionProposal, ActionType

    e = town
    with e.db.write() as c:
        cause = c.execute(
            "SELECT id FROM world_events WHERE world_id=? AND occurred_at<=? "
            "ORDER BY occurred_at DESC LIMIT 1", (e.wid, e.at.isoformat()),
        ).fetchone()
        cause_id = cause["id"] if cause else None
        before = c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        outcome = ActionService().execute(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            proposal=ActionProposal(
                actor_id=e.npc, action=ActionType.IDLE, reason="查看周围",
                metadata={"cause_event_ids": [cause_id] if cause_id else []},
            ),
        )
        effect = c.execute(
            "SELECT * FROM action_effects WHERE source_event_id=?", (outcome.event_id,),
        ).fetchone()
        assert effect is not None
        assert effect["status"] == "applied"
        assert json.loads(effect["payload_json"])["observed"]["money"] == before
        if cause_id:
            assert c.execute(
                "SELECT 1 FROM event_causes WHERE event_id=? AND cause_event_id=?",
                (outcome.event_id, cause_id),
            ).fetchone()
        rejected = ActionService().execute(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            proposal=ActionProposal(actor_id=e.npc, action=ActionType.TRAVEL,
                                    reason="试行", destination_id="missing"),
        )
        assert not rejected.accepted
        failed_effect = c.execute(
            "SELECT * FROM action_effects WHERE source_event_id=?", (rejected.event_id,),
        ).fetchone()
        assert failed_effect["status"] == "rejected"
        assert json.loads(failed_effect["payload_json"])["reason"]


def test_transfer_action_records_both_inventories(town):
    import json
    from uuid import uuid4

    from world_engine.actions import ActionService
    from world_engine.domain import ActionProposal, ActionType

    e = town
    with e.db.write() as c:
        outcome = ActionService().execute(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            proposal=ActionProposal(actor_id=e.npc, action=ActionType.TRANSFER,
                                    target_id=e.player, reason="送一份食物",
                                    metadata={"item": "旅行面包", "quantity": 1}),
        )
        assert outcome.accepted, outcome.summary
        effect = c.execute(
            "SELECT payload_json FROM action_effects WHERE source_event_id=?", (outcome.event_id,),
        ).fetchone()
        changes = json.loads(effect[0])["changes"]
        assert "items" in changes
        assert "target_items" in changes


def test_every_action_type_returns_audited_outcome(town):
    from uuid import uuid4

    from world_engine.actions import ActionService
    from world_engine.domain import ActionProposal, ActionType

    e = town
    with e.db.write() as c:
        for action in ActionType:
            outcome = ActionService().execute(
                c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
                proposal=ActionProposal(actor_id=e.npc, action=action,
                                        reason="动作覆盖审计"),
            )
            assert outcome.event_id
            assert c.execute(
                "SELECT 1 FROM action_effects WHERE source_event_id=?",
                (outcome.event_id,),
            ).fetchone(), action


def test_work_completion_returns_verified_result_to_goal(town):
    import json

    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=0 WHERE id=?", (e.npc,))
        goal_id = NpcAgencyService.adopt(
            c, e.wid, e.npc, "earn_money", {"kind": "money_at_least", "amount": 12},
            "为旅途准备经费", e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    response = e.client.get(f"/api/worlds/{e.wid}/npc-agency/{e.npc}")
    assert response.status_code == 200
    goal = next(row for row in response.json()["goals"] if row["id"] == goal_id)
    step = goal["steps"][0]
    assert step["status"] == "completed"
    assert step["result_event_id"] != step["event_id"]
    assert step["event_id"] in {cause["cause_event_id"] for cause in step["result_causes"]}
    result = step["result_effects"][0]["payload"]
    assert result["changes"]["money"]["after"] == 12
    assert result["goal_id"] == goal_id
    with e.db.read() as c:
        effect = c.execute("SELECT payload_json FROM action_effects WHERE source_event_id=?",
                           (step["result_event_id"],)).fetchone()
        assert json.loads(effect[0])["observed"]["activity"] is None


def test_arrival_is_result_of_goal_travel(town):
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.movement import MovementService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        destination = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,?,'wilds',?,?)",
            (destination, e.wid, "调查目的地", e.loc["longitude"] + .015, e.loc["latitude"]),
        )
        goal_id = NpcAgencyService.adopt(
            c, e.wid, e.npc, "travel", {"kind": "at_location", "location_id": destination},
            "核对已经掌握的道路信息", e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        MovementService().advance(
            c, world_id=e.wid, previous_time=e.at,
            current_time=e.at + timedelta(hours=3), created_at=e.at,
        )
    goal = next(row for row in e.client.get(
        f"/api/worlds/{e.wid}/npc-agency/{e.npc}",
    ).json()["goals"] if row["id"] == goal_id)
    step = goal["steps"][0]
    assert step["status"] == "completed"
    assert "抵达" in step["result_summary"]
    assert step["event_id"] in {cause["cause_event_id"] for cause in step["result_causes"]}
    assert step["result_effects"][0]["payload"]["changes"]["longitude"]["after"] == e.loc["longitude"] + .015


def test_inventory_goal_composes_harvest_craft_and_collection(town):
    from datetime import timedelta

    from test_npc_goals import crafting

    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    craft, material, product = crafting(e)
    with e.db.write() as c:
        goal_id = NpcAgencyService.adopt(
            c, e.wid, e.npc, "personal_inventory",
            {"kind": "inventory_at_least", "item_type_id": product, "amount": 1,
             "recipe_id": craft["recipe_id"]}, "为自己制作旅行用品", e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=? "
                         "AND container_id=?", (material, e.npc)).fetchone()[0] == 1
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        activity = LifeActivityService.running(c, e.npc)
        assert activity and activity["kind"] == "craft"
        LifeActivityService.finish(c, activity, e.at + timedelta(hours=2), "completed", "工序完成")
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=2))
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=? "
                         "AND container_id=? AND container_type='character_inventory'",
                         (product, e.npc)).fetchone()[0] == 1
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=3)) is None
        assert c.execute("SELECT status FROM npc_agency_goals WHERE id=?", (goal_id,)).fetchone()[0] == "completed"
        assert [row[0] for row in c.execute(
            "SELECT action_type FROM npc_agency_steps WHERE goal_id=? ORDER BY world_time",
            (goal_id,),
        )] == ["harvest", "activity", "collect_output"]


def test_worker_keeps_collection_in_the_autonomous_goal_chain(town):
    from test_npc_goals import crafting

    from world_engine.npc_agency import NpcAgencyService

    e = town
    craft, _, product = crafting(e)
    with e.db.write() as c:
        goal = NpcAgencyService.adopt(c, e.wid, e.npc, "personal_inventory",
                                     {"kind": "inventory_at_least", "item_type_id": product,
                                      "amount": 1, "recipe_id": craft["recipe_id"]},
                                     "为自己制作旅行用品", e.at)
    for _ in range(4):
        e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        steps = [row[0] for row in c.execute("SELECT action_type FROM npc_agency_steps WHERE goal_id=? "
                                             "ORDER BY world_time", (goal,))]
        assert steps == ["harvest", "activity", "collect_output"]
        assert c.execute("SELECT status FROM npc_agency_goals WHERE id=?", (goal,)).fetchone()[0] == "completed"


def test_goal_records_trigger_and_verified_completion_once(town):
    import json
    from datetime import timedelta

    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=0 WHERE id=?", (e.npc,))
        goal = NpcAgencyService.adopt(
            c, e.wid, e.npc, "saving", {"kind": "money_at_least", "amount": 9},
            "准备购买生活用品", e.at,
        )
        adopted = c.execute("SELECT payload_json FROM world_events WHERE event_type='agency.goal_adopted' "
                            "AND json_extract(payload_json,'$.goal_id')=?", (goal,)).fetchone()
        assert json.loads(adopted[0])["trigger_state"]["money"] == 0
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        activity = LifeActivityService.running(c, e.npc)
        LifeActivityService.finish(c, activity, e.at + timedelta(hours=1), "completed", "工作完成")
        NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=2))
        completed = c.execute("SELECT id,payload_json FROM world_events WHERE event_type='agency.goal_completed' "
                              "AND json_extract(payload_json,'$.goal_id')=?", (goal,)).fetchall()
        assert len(completed) == 1
        assert json.loads(completed[0]["payload_json"])["observed"]["money"] >= 9
        assert c.execute("SELECT 1 FROM event_causes WHERE event_id=?", (completed[0]["id"],)).fetchone()


def test_long_term_savings_makes_verified_progress_beyond_search_window(town):
    from datetime import timedelta

    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=0 WHERE id=?", (e.npc,))
        goal = NpcAgencyService.adopt(c, e.wid, e.npc, "long_saving",
                                     {"kind": "money_at_least", "amount": 500}, "攒钱购买远行装备", e.at,
                                     priority=2)
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 0
        activity = LifeActivityService.running(c, e.npc)
        assert activity["kind"] == "work"
        wage = c.execute("SELECT wage FROM workplace_accounts WHERE world_id=? AND location_id=?",
                         (e.wid, activity["location_id"])).fetchone()[0]
        LifeActivityService.finish(c, activity, e.at + timedelta(hours=1), "completed", "工作完成")
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == wage
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        assert c.execute("SELECT status FROM npc_agency_goals WHERE id=?", (goal,)).fetchone()[0] == "active"


def test_location_field_does_not_replace_travel_and_high_energy_rest(town):
    from world_engine.domain import ActionType
    from world_engine.npc_operators import next_action

    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+.015,energy=70 WHERE id=?", (e.npc,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        planned = next_action(c, actor, {"kind": "at_location", "location_id": e.loc["id"]}, "回家", e.at)
        assert planned.action == ActionType.TRAVEL
        planned = next_action(c, actor, {"kind": "energy_at_least", "amount": 90}, "养足精神", e.at)
        assert planned and planned.action == ActionType.REST


def test_health_goal_gathers_and_uses_owned_medicine(town):
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        medicine, item = str(uuid4()), str(uuid4())
        c.execute("INSERT INTO item_types(id,name,category,heal) VALUES (?,'随身伤药','consumable',30)", (medicine,))
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition) "
                  "VALUES (?,?,?,'location_ground',?,1,100)", (item, e.wid, medicine, e.loc["id"]))
        c.execute("UPDATE characters SET health=50 WHERE id=?", (e.npc,))
        goal = NpcAgencyService.adopt(c, e.wid, e.npc, "recover",
                                     {"kind": "health_at_least", "amount": 80}, "治好旅行中的伤", e.at)
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert c.execute("SELECT health FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 50
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        assert c.execute("SELECT health FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 80
        NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=2))
        assert c.execute("SELECT status FROM npc_agency_goals WHERE id=?", (goal,)).fetchone()[0] == "completed"


def test_injured_npc_forms_recovery_goal_on_heartbeat(town):
    from uuid import uuid4

    e = town
    with e.db.write() as c:
        medicine = str(uuid4())
        c.execute("INSERT INTO item_types(id,name,category,heal) VALUES (?,'治疗伤药','consumable',30)", (medicine,))
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) "
                  "VALUES (?,?,?,'character_inventory',?,1,100,?)", (str(uuid4()), e.wid, medicine, e.npc, e.npc))
        c.execute("UPDATE characters SET health=35 WHERE id=?", (e.npc,))
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        goal = c.execute("SELECT id FROM npc_agency_goals WHERE character_id=? "
                         "AND json_extract(predicate_json,'$.kind')='health_at_least'", (e.npc,)).fetchone()
        assert goal
        assert c.execute("SELECT 1 FROM npc_agency_steps WHERE goal_id=? AND action_type='use' "
                         "AND status='accepted'", (goal["id"],)).fetchone()
        assert c.execute("SELECT health FROM characters WHERE id=?", (e.npc,)).fetchone()[0] >= 60


def test_delivery_goal_composes_production_and_transfer(town):
    from datetime import timedelta

    from test_npc_goals import crafting

    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    craft, _, product = crafting(e)
    with e.db.write() as c:
        goal = NpcAgencyService.adopt(
            c, e.wid, e.npc, "deliver",
            {"kind": "delivered_item", "item_type_id": product, "amount": 1,
             "recipe_id": craft["recipe_id"], "target_id": e.player, "location_id": e.loc["id"]},
            "制作并赠送一只旅行木盒", e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        LifeActivityService.finish(c, LifeActivityService.running(c, e.npc), e.at + timedelta(hours=2), "completed", "工序完成")
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=2))
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=3))
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=? AND container_id=? "
                         "AND container_type='character_inventory'", (product, e.player)).fetchone()[0] == 1
        NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=4))
        assert c.execute("SELECT status FROM npc_agency_goals WHERE id=?", (goal,)).fetchone()[0] == "completed"


def test_repair_goal_gets_material_and_waits_for_real_check(town, monkeypatch):
    import json
    from datetime import timedelta
    from uuid import uuid4

    from test_npc_goals import crafting

    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    craft, _, product = crafting(e)
    monkeypatch.setattr("world_engine.action_checks.draw", lambda: 1)
    with e.db.write() as c:
        recipe = c.execute("SELECT * FROM activity_recipes WHERE id=?", (craft["recipe_id"],)).fetchone()
        spec = json.loads(recipe["spec_json"])
        spec.update(kind="repair", output_item_type_id=None, repair_category="tool", check_difficulty=0)
        c.execute("UPDATE activity_recipes SET spec_json=? WHERE id=?", (json.dumps(spec), recipe["id"]))
        item = str(uuid4())
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) "
                  "VALUES (?,?,?,'character_inventory',?,1,40,?)", (item, e.wid, product, e.npc, e.npc))
        goal = NpcAgencyService.adopt(c, e.wid, e.npc, "repair_tool",
                                     {"kind": "item_condition_at_least", "item_id": item, "amount": 60,
                                      "recipe_id": recipe["id"]}, "修好随身工具", e.at)
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        assert c.execute("SELECT condition FROM item_instances WHERE id=?", (item,)).fetchone()[0] == 40
        LifeActivityService.finish(c, LifeActivityService.running(c, e.npc), e.at + timedelta(hours=2), "completed", "修理结束")
        assert c.execute("SELECT condition FROM item_instances WHERE id=?", (item,)).fetchone()[0] >= 60
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=2))
        NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=3))
        assert c.execute("SELECT status FROM npc_agency_goals WHERE id=?", (goal,)).fetchone()[0] == "completed"


def test_location_opportunity_remembers_verified_visit_after_return(town):
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.movement import MovementService
    from world_engine.npc_agency import NpcAgencyService
    from world_engine.opportunities import OpportunityService

    e = town
    with e.db.write() as c:
        remote = str(uuid4())
        c.execute("INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
                  "VALUES (?,?,?,'wilds',?,?)",
                  (remote, e.wid, "道路巡查点", e.loc["longitude"] + .015, e.loc["latitude"]))
        c.execute("UPDATE characters SET identity='冒险者' WHERE id=?", (e.npc,))
        opportunity = OpportunityService.publish(
            c, e.wid, e.player, e.loc["id"], remote,
            {"kind": "at_location", "location_id": remote}, 9, e.at,
        )
        for hours in (0, 1, 3, 5):
            if hours in (3, 5):
                MovementService().advance(
                    c, world_id=e.wid, previous_time=e.at + timedelta(hours=hours - 2),
                    current_time=e.at + timedelta(hours=hours), created_at=e.at,
                )
            assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=hours))
        assert c.execute("SELECT status FROM npc_opportunities WHERE id=?",
                         (opportunity,)).fetchone()[0] == "completed"


def test_world_heartbeat_creates_npc_goal_without_player_command(town):
    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=0,satiety=100,energy=90 WHERE id=?", (e.npc,))
    e.clock.heartbeat(e.wid, elapsed_seconds=3600)
    with e.db.read() as c:
        goals = NpcAgencyService.view(c, e.npc)
        livelihood = next(goal for goal in goals if goal["kind"] == "livelihood")
        assert c.execute(
            "SELECT 1 FROM npc_agency_steps WHERE goal_id=? AND event_id IS NOT NULL",
            (livelihood["id"],),
        ).fetchone()


def test_goal_executes_and_records_real_steps(town):
    from datetime import timedelta

    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET money=0 WHERE id=?", (e.npc,))
        goal = NpcAgencyService.adopt(
            c, e.wid, e.npc, "earn_money", {"kind": "money_at_least", "amount": 12},
            "想攒够购买用品的钱", e.at,
        )
        first = NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert first
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at) is None
        steps = c.execute("SELECT * FROM npc_agency_steps WHERE goal_id=?", (goal,)).fetchall()
        assert len(steps) == 1
        assert steps[0]["event_id"]
        assert steps[0]["status"] == "accepted"
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == 0
        assert c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone() or c.execute("SELECT 1 FROM character_life_activities WHERE character_id=? AND status='running'", (e.npc,)).fetchone()
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(minutes=1)) is None


def test_food_goal_uses_real_work_purchase_and_eating(town):
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        buyer = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute(
            "UPDATE characters SET money=0,satiety=35,energy=90,identity='旅人',"
            "location_id=?,current_location_id=?,longitude=?,latitude=? WHERE id=?",
            (e.loc["id"], e.loc["id"], e.loc["longitude"], e.loc["latitude"], buyer),
        )
        c.execute("DELETE FROM item_instances WHERE owner_character_id=?", (buyer,))
        c.execute("UPDATE world_item_profiles SET resource_owner_id=? "
                  "WHERE world_id=? AND nutrition>0", (e.npc, e.wid))
        goal = NpcAgencyService.adopt(
            c, e.wid, buyer, "self_care", {"kind": "satiety_at_least", "amount": 65},
            "想吃上一顿饭", e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, buyer, e.at)
        heartbeat_id = str(uuid4())
        c.execute(
            "INSERT INTO world_heartbeats(id,world_id,real_time,real_elapsed_seconds,"
            "time_scale,world_delta_seconds,world_time_before,world_time_after,created_at) "
            "VALUES (?,?,?,0,1,10800,?,?,?)",
            (heartbeat_id, e.wid, e.at.isoformat(), e.at.isoformat(),
             (e.at + timedelta(hours=3)).isoformat(), e.at.isoformat()),
        )
        LifeActivityService.advance(c, e.wid, e.at + timedelta(hours=3), heartbeat_id)
        assert c.execute("SELECT money FROM characters WHERE id=?", (buyer,)).fetchone()[0] > 0
        second = NpcAgencyService.tick(c, e.wid, buyer, e.at + timedelta(hours=3))
        assert second, {"activities": [dict(row) for row in c.execute(
            "SELECT kind,status,ends_world_time FROM character_life_activities WHERE character_id=?", (buyer,))],
            "goals": [(row["status"], row["retry_world_time"], row["predicate"])
                      for row in NpcAgencyService.view(c, buyer)],
            "satiety": c.execute("SELECT satiety FROM characters WHERE id=?", (buyer,)).fetchone()[0]}
        assert NpcAgencyService.tick(c, e.wid, buyer, e.at + timedelta(hours=4))
        assert c.execute("SELECT satiety FROM characters WHERE id=?", (buyer,)).fetchone()[0] >= 65
        NpcAgencyService.tick(c, e.wid, buyer, e.at + timedelta(hours=5))
        steps = c.execute(
            "SELECT action_type,event_id FROM npc_agency_steps WHERE goal_id=? ORDER BY world_time",
            (goal,),
        ).fetchall()
        assert [step["action_type"] for step in steps] == ["work", "buy_food", "eat"]
        assert all(step["event_id"] for step in steps)


def test_news_creates_goal_only_after_npc_learns_it(town):
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.actions import ActionService
    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        away = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,?,'place',?,?)",
            (away, e.wid, "附近营地", e.loc["longitude"] + .015, e.loc["latitude"]),
        )
        c.execute(
            "UPDATE characters SET current_location_id=?,location_id=?,longitude=?,latitude=?,"
            "money=200,satiety=100,identity='调查员' WHERE id=?",
            (away, away, e.loc["longitude"] + .015, e.loc["latitude"], e.npc),
        )
        event_id = ActionService._record_event(
            c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="action.attack", actor_id=e.player, target_id=None,
            location_id=e.loc["id"], summary="附近发生了冲突", payload={},
        )
        NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert not [g for g in NpcAgencyService.view(c, e.npc) if g["kind"] == "investigate"]
        heard_at = e.at + timedelta(minutes=1)
        c.execute(
            "INSERT INTO character_event_knowledge(character_id,event_id,world_id,source_kind,"
            "confidence,learned_world_time) VALUES (?,?,?,'rumor',0.8,?)",
            (e.npc, event_id, e.wid, heard_at.isoformat()),
        )
        NpcAgencyService.tick(c, e.wid, e.npc, heard_at)
        goal = next(g for g in NpcAgencyService.view(c, e.npc) if g["kind"] == "investigate")
        assert goal["source_event_id"] == event_id
        adoption = c.execute(
            "SELECT id FROM world_events WHERE actor_id=? AND event_type='agency.goal_adopted' "
            "AND json_extract(payload_json,'$.goal_id')=?", (e.npc, goal["id"]),
        ).fetchone()[0]
        assert c.execute(
            "SELECT 1 FROM event_causes WHERE event_id=? AND cause_event_id=?",
            (adoption, event_id),
        ).fetchone()
        step = c.execute("SELECT * FROM npc_agency_steps WHERE goal_id=?", (goal["id"],)).fetchone()
        assert step["action_type"] == "travel"
        assert c.execute(
            "SELECT 1 FROM event_causes WHERE event_id=? AND cause_event_id=?",
            (step["event_id"], adoption),
        ).fetchone()
        from world_engine.movement import MovementService
        MovementService().advance(
            c, world_id=e.wid, previous_time=heard_at,
            current_time=heard_at + timedelta(hours=2), created_at=heard_at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, heard_at + timedelta(hours=2))
        actions = [row["action_type"] for row in c.execute(
            "SELECT action_type FROM npc_agency_steps WHERE goal_id=? ORDER BY world_time", (goal["id"],))]
        assert actions == ["travel", "socialize"]
        NpcAgencyService.tick(c, e.wid, e.npc, heard_at + timedelta(hours=3))
        assert next(g for g in NpcAgencyService.view(c, e.npc) if g["id"] == goal["id"])["status"] == "completed"


def test_food_goal_can_harvest_when_buying_is_unaffordable(town):
    from datetime import timedelta

    from world_engine.npc_agency import NpcAgencyService

    e = town
    with e.db.write() as c:
        buyer = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute(
            "UPDATE characters SET money=0,satiety=35,energy=90,identity='旅人',"
            "location_id=?,current_location_id=?,longitude=?,latitude=? WHERE id=?",
            (e.loc["id"], e.loc["id"], e.loc["longitude"], e.loc["latitude"], buyer),
        )
        c.execute("DELETE FROM item_instances WHERE owner_character_id=?", (buyer,))
        c.execute("UPDATE world_item_profiles SET resource_owner_id=NULL WHERE item_type_id=?",
                  (e.food,))
        goal = NpcAgencyService.adopt(
            c, e.wid, buyer, "self_care", {"kind": "satiety_at_least", "amount": 65},
            "想吃上一顿饭", e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, buyer, e.at)
        assert NpcAgencyService.tick(c, e.wid, buyer, e.at + timedelta(hours=1))
        assert [row[0] for row in c.execute(
            "SELECT action_type FROM npc_agency_steps WHERE goal_id=? ORDER BY world_time", (goal,))
        ] == ["harvest_food", "eat"]


def test_public_opportunity_executes_and_pays_once(town):
    from datetime import timedelta
    from uuid import uuid4

    from world_engine.movement import MovementService
    from world_engine.npc_agency import NpcAgencyService
    from world_engine.opportunities import OpportunityService

    e = town
    with e.db.write() as c:
        target = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        wilds = str(uuid4())
        c.execute(
            "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
            "VALUES (?,?,?,'wilds',?,?)",
            (wilds, e.wid, "野兽出没地", e.loc["longitude"] + .015, e.loc["latitude"]),
        )
        c.execute(
            "UPDATE characters SET health=1,identity='野兽',current_location_id=?,"
            "location_id=?,longitude=?,latitude=? WHERE id=?",
            (wilds, wilds, e.loc["longitude"] + .015, e.loc["latitude"], target),
        )
        c.execute("UPDATE characters SET identity='冒险者',money=200,satiety=100 WHERE id=?",
                  (e.npc,))
        issuer_before = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
        opportunity_id = OpportunityService.publish(
            c, e.wid, e.player, e.loc["id"], wilds,
            {"kind": "target_health_at_most", "target_id": target, "amount": 0}, 9, e.at,
        )
        source = c.execute(
            "SELECT source_event_id FROM npc_opportunities WHERE id=?", (opportunity_id,),
        ).fetchone()[0]
        assert c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (e.npc, source),
        ).fetchone()
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        assert c.execute("SELECT status FROM npc_opportunities WHERE id=?", (opportunity_id,)).fetchone()[0] == "accepted"
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1))
        MovementService().advance(
            c, world_id=e.wid, previous_time=e.at + timedelta(hours=1),
            current_time=e.at + timedelta(hours=3), created_at=e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=3))
        assert c.execute("SELECT health FROM characters WHERE id=?", (target,)).fetchone()[0] == 0
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=4))
        MovementService().advance(
            c, world_id=e.wid, previous_time=e.at + timedelta(hours=4),
            current_time=e.at + timedelta(hours=6), created_at=e.at,
        )
        adventurer_before = c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=6))
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == adventurer_before + 9, {
            "steps": [tuple(row) for row in c.execute(
                "SELECT action_type,status,reason FROM npc_agency_steps WHERE goal_id IN "
                "(SELECT id FROM npc_agency_goals WHERE character_id=?) ORDER BY world_time", (e.npc,))],
            "position": tuple(c.execute("SELECT longitude,latitude,current_location_id FROM characters WHERE id=?", (e.npc,)).fetchone()),
        }
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == issuer_before - 9
        payout = OpportunityService.claim(c, opportunity_id, e.npc, e.at + timedelta(hours=7))
        assert payout == c.execute("SELECT payout_event_id FROM npc_opportunities WHERE id=?", (opportunity_id,)).fetchone()[0]
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == adventurer_before + 9
        goal = next(g for g in NpcAgencyService.view(c, e.npc) if g["kind"] == "opportunity")
        assert [row["action_type"] for row in c.execute(
            "SELECT action_type FROM npc_agency_steps WHERE goal_id=? ORDER BY world_time", (goal["id"],))] == [
                "accept_opportunity", "travel", "attack", "travel", "claim_opportunity"]
        events = c.execute(
            "SELECT source_event_id,accept_event_id,payout_event_id "
            "FROM npc_opportunities WHERE id=?", (opportunity_id,),
        ).fetchone()
        for event_id in events:
            assert c.execute(
                "SELECT COUNT(*) FROM action_effects WHERE source_event_id=?", (event_id,),
            ).fetchone()[0] == 1


def test_opportunity_cancels_without_claim_when_target_lost(town):
    from datetime import timedelta

    from world_engine.npc_agency import NpcAgencyService
    from world_engine.opportunities import OpportunityService

    e = town
    with e.db.write() as c:
        target = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 1",
            (e.wid, e.npc),
        ).fetchone()["id"]
        c.execute("UPDATE characters SET identity='冒险者',money=200,satiety=100 WHERE id=?",
                  (e.npc,))
        before = c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0]
        opportunity_id = OpportunityService.publish(
            c, e.wid, e.player, e.loc["id"], e.loc["id"],
            {"kind": "target_health_at_most", "target_id": target, "amount": 0},
            9, e.at,
        )
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at)
        c.execute("UPDATE characters SET health=0 WHERE id=?", (target,))
        assert NpcAgencyService.tick(c, e.wid, e.npc, e.at + timedelta(hours=1)) is None
        assert c.execute("SELECT status FROM npc_opportunities WHERE id=?",
                         (opportunity_id,)).fetchone()[0] == "cancelled"
        assert c.execute("SELECT money FROM characters WHERE id=?", (e.player,)).fetchone()[0] == before
        goal = next(g for g in NpcAgencyService.view(c, e.npc) if g["kind"] == "opportunity")
        assert goal["status"] == "failed"
