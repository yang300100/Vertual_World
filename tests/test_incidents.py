# ruff: noqa: F811
"""Yuki 检查全球事件必须由人物知情、协作和真实维修解除。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import town  # noqa: F401
from test_npc_goals import crafting

from world_engine.economy import EconomyService
from world_engine.epistemics import KnowledgeService
from world_engine.event_log import record_event
from world_engine.npc_agency import NpcAgencyService
from world_engine.repository import to_iso


@pytest.fixture
def crisis(town):
    from world_engine.incidents import IncidentService

    e = town
    craft, material, product = crafting(e, source=False)
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (e.at.isoformat(), e.wid))
        recipe = c.execute("SELECT spec_json FROM activity_recipes WHERE id=?",
                           (craft["recipe_id"],)).fetchone()
        spec = json.loads(recipe[0])
        spec.update(kind="repair", output_item_type_id=None, repair_category="tool",
                    repair_points=50, check_difficulty=0, check_min_proficiency=10)
        c.execute("UPDATE activity_recipes SET spec_json=? WHERE id=?",
                  (json.dumps(spec), craft["recipe_id"]))
        helper = c.execute("SELECT id FROM characters WHERE world_id=? AND is_player=0 "
                           "AND id!=? LIMIT 1", (e.wid, e.npc)).fetchone()[0]
        camp = str(uuid4())
        c.execute("INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
                  "VALUES (?,?,?,'place',?,?)",
                  (camp, e.wid, "运料营地", e.loc["longitude"] + .004, e.loc["latitude"]))
        c.execute("UPDATE characters SET identity='运料人',current_location_id=?,location_id=?,"
                  "longitude=?,latitude=?,skills_json='[]' WHERE id=?",
                  (camp, camp, e.loc["longitude"] + .004, e.loc["latitude"], helper))
        c.execute("UPDATE characters SET identity='滤网维修工',skills_json='[\"修理\"]' WHERE id=?",
                  (e.npc,))
        c.execute("INSERT OR REPLACE INTO character_skill_proficiencies"
                  "(character_id,world_id,skill_name,proficiency,updated_at) VALUES (?,?,'修理',60,?)",
                  (e.npc, e.wid, e.at.isoformat()))
        item = str(uuid4())
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                  "container_id,owner_character_id,quantity,condition) "
                  "VALUES (?,?,?,'character_inventory',?,?,1,100)",
                  (item, e.wid, product, e.npc, e.npc))
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                  "container_id,owner_character_id,quantity,condition) "
                  "VALUES (?,?,?,'character_inventory',?,?,1,100)",
                  (str(uuid4()), e.wid, material, helper, helper))
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,"
                  "container_id,owner_character_id,quantity,condition) "
                  "VALUES (?,?,?,'character_inventory',?,?,20,100)",
                  (str(uuid4()), e.wid, e.food, helper, helper))
        address = record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=e.at,
            event_type="world.test_address_known", actor_id=e.npc,
            summary="工坊运料名单记载同事在运料营地", payload={})
        KnowledgeService.add_evidence(c, observer_id=e.npc, subject_id=helper,
            predicate="location", value=camp, event_id=address, method="document",
            quote="同事在运料营地")
        incident = IncidentService.create(c, e.wid, "全球灰幕", "火山灰阻碍露天粮食生产",
            [{"location_id": e.loc["id"], "custodian_id": e.npc, "item_id": item,
              "recipe_id": craft["recipe_id"], "resource_keys": ["bakery_supply"],
              "required_condition": 80, "damaged_condition": 40}], e.at,
            idempotency_key="gray-veil")
    return e, incident, helper, item, material


def test_incident_restricts_production_and_only_local_witnesses_know(crisis):
    from world_engine.incidents import IncidentService

    e, incident, helper, item, _ = crisis
    with e.db.write() as c:
        assert IncidentService.create(c, e.wid, "忽略重复请求", "重复", [], e.at,
                                      idempotency_key="gray-veil") == incident
        IncidentService.tick(c, e.wid, e.at)
        site = IncidentService.view(c, e.wid)[0]["sites"][0]
        assert c.execute("SELECT condition FROM item_instances WHERE id=?", (item,)).fetchone()[0] == 40
        assert c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                         (e.npc, site["observation_event_id"])).fetchone()
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                             (helper, site["observation_event_id"])).fetchone()
        NpcAgencyService.seed(c, e.wid, c.execute("SELECT * FROM characters WHERE id=?", (helper,)).fetchone(), e.at)
        assert not [g for g in NpcAgencyService.view(c, helper) if g["kind"] == "incident_supply"]
        c.execute("UPDATE locations SET resources_json='{}' WHERE id=?", (e.loc["id"],))
        c.execute("UPDATE world_item_profiles SET last_growth_world_time=? WHERE item_type_id=?",
                  (e.at.isoformat(), e.food))
        EconomyService.tick(c, e.wid, e.at + timedelta(days=1))
        assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc["id"],)).fetchone()[0]).get("bakery_supply", 0) == 0


def test_heartbeat_npcs_hear_request_deliver_and_repair_without_player(crisis):
    from world_engine.incidents import IncidentService

    e, incident, helper, item, material = crisis
    for hour in range(1, 73):
        e.clock.heartbeat(e.wid, elapsed_seconds=3600)
        with e.db.read() as c:
            view = IncidentService.view(c, e.wid)[0]
            if view["status"] == "resolved":
                break
    with e.db.read() as c:
        diagnostic = {"view": view, "goals": [dict(r) for r in c.execute(
            "SELECT character_id,kind,status,next_step_json FROM npc_agency_goals")],
            "events": [tuple(r) for r in c.execute(
            "SELECT event_type,actor_id FROM world_events WHERE event_type LIKE '%incident%'")],
            "repairs": [dict(r) for r in c.execute("SELECT a.source_event_id,a.finish_event_id,"
            "a.status,a.kind,d.spec_json,d.result_json FROM character_life_activities a "
            "JOIN activity_task_details d ON d.activity_id=a.id WHERE a.kind='repair'")],
            "clock": c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (e.wid,)).fetchone()[0]}
    assert view["status"] == "resolved", json.dumps(diagnostic, ensure_ascii=False, indent=2)
    with e.db.write() as c:
        site = view["sites"][0]
        assert site["request_event_id"] and site["resolution_event_id"]
        goals = NpcAgencyService.view(c, helper)
        delivery = next(g for g in goals if g["kind"] == "incident_supply")
        assert delivery["source_event_id"] == site["request_event_id"]
        assert c.execute("SELECT source_kind FROM character_event_knowledge WHERE character_id=? "
                         "AND event_id=?", (helper, site["request_event_id"])).fetchone()[0] in {"heard", "experienced"}
        assert [r[0] for r in c.execute("SELECT action_type FROM npc_agency_steps WHERE goal_id=? AND status!='rejected' "
                                       "ORDER BY world_time", (delivery["id"],))] == ["travel", "transfer"]
        repair = next(g for g in NpcAgencyService.view(c, e.npc) if g["kind"] == "incident_repair")
        assert repair["status"] == "completed"
        assert c.execute("SELECT condition FROM item_instances WHERE id=?", (item,)).fetchone()[0] >= 80
        assert c.execute("SELECT COUNT(*) FROM item_instances WHERE item_type_id=? AND quantity>0",
                         (material,)).fetchone()[0] == 0
        assert c.execute("SELECT 1 FROM event_causes WHERE event_id=?",
                         (view["resolution_event_id"],)).fetchone()
        IncidentService.tick(c, e.wid, e.at + timedelta(hours=hour + 1))
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE event_type='world.incident_resolved' "
                         "AND json_extract(payload_json,'$.incident_id')=?", (incident,)).fetchone()[0] == 1
        c.execute("UPDATE locations SET resources_json='{}' WHERE id=?", (e.loc["id"],))
        resolved = e.at + timedelta(hours=hour)
        c.execute("UPDATE world_item_profiles SET last_growth_world_time=? WHERE item_type_id=?",
                  (resolved.isoformat(), e.food))
        EconomyService.tick(c, e.wid, resolved + timedelta(days=1))
        assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?",
                                   (e.loc["id"],)).fetchone()[0])["bakery_supply"] == 20


def test_editing_item_condition_does_not_forge_incident_resolution(crisis):
    from world_engine.incidents import IncidentService

    e, _, _, item, _ = crisis
    with e.db.write() as c:
        c.execute("UPDATE item_instances SET condition=100 WHERE id=?", (item,))
        IncidentService.tick(c, e.wid, e.at + timedelta(days=1))
        assert IncidentService.view(c, e.wid)[0]["status"] == "active"


def test_incidents_read_api(crisis):
    e, incident, _, _, _ = crisis
    response = e.client.get(f"/api/worlds/{e.wid}/incidents")
    assert response.status_code == 200
    assert response.json()[0]["id"] == incident


def test_global_scenario_requires_every_region_and_no_model_api(database, settings, monkeypatch):
    from scripts.demo_global_incident import build_scenario, simulate

    def forbid_model(*args, **kwargs):
        pytest.fail("哥哥的模型 API 不能用于离线验收")

    monkeypatch.setattr("httpx.Client.send", forbid_model)
    monkeypatch.setattr("httpx.AsyncClient.send", forbid_model)
    wid, _, at = build_scenario(database)
    report = simulate(database, settings, wid, at)
    assert report["status"] == "resolved", report["steps"]
    assert len(report["sites"]) == 3
    assert report["model_api_calls"] == 0
    assert all(s["resolution_event_id"] for s in report["sites"])
    # 哥哥，多个地区可以在同一心跳恢复，验收要检查全局完成的因果条件。
    assert all(p["status"] == "active" for p in report["progress"] if p["restored_sites"] < 3)
    assert report["progress"][-1]["restored_sites"] == 3
    assert report["progress"][-1]["status"] == "resolved"
    assert {cause["cause_event_id"] for cause in report["causes"]
            if cause["event_id"] == report["resolution_event_id"]} == {
                site["resolution_event_id"] for site in report["sites"]}
    assert len(report["transmissions"]) >= 3
    assert all(t["event_type"] != "social.public_rumor" for t in report["transmissions"])
    assert sum(s["action_type"] == "socialize" for s in report["steps"]) >= 3
    assert sum(s["action_type"] == "transfer" and s["status"] == "accepted"
               for s in report["steps"]) == 3
    assert all(p["productive_days"] == 1 for p in report["production_after_resolution"])
    assert len({r["location_id"] for r in report["regeneration_events"]}) == 3
    assert report["causes"]


def test_production_does_not_catch_up_for_disaster_days(crisis):
    e, _, _, _, _ = crisis
    with e.db.write() as c:
        c.execute("UPDATE locations SET resources_json='{}' WHERE id=?", (e.loc["id"],))
        c.execute("UPDATE world_item_profiles SET last_growth_world_time=? WHERE item_type_id=?",
                  (e.at.isoformat(), e.food))
        EconomyService.tick(c, e.wid, e.at + timedelta(days=2))
        c.execute("UPDATE incident_sites SET resolved_world_time=?",
                  ((e.at + timedelta(days=2)).isoformat(),))
        c.execute("UPDATE incident_disruptions SET resolved_world_time=?",
                  ((e.at + timedelta(days=2)).isoformat(),))
        EconomyService.tick(c, e.wid, e.at + timedelta(days=3))
        resources = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?",
                                        (e.loc["id"],)).fetchone()[0])
        assert resources["bakery_supply"] == 20


def test_invalid_incident_api_is_atomic(crisis):
    e, _, helper, item, _ = crisis
    before = e.client.get(f"/api/worlds/{e.wid}/incidents").json()
    response = e.client.post(f"/api/worlds/{e.wid}/incidents", json={
        "title": "错误危机", "description": "错误保管人不能破坏原件",
        "idempotency_key": "invalid-owner", "sites": [{
            "location_id": e.loc["id"], "custodian_id": helper, "item_id": item,
            "recipe_id": before[0]["sites"][0]["recipe_id"], "resource_keys": ["bakery_supply"],
            "required_condition": 80, "damaged_condition": 20,
        }],
    })
    assert response.status_code == 400
    with e.db.read() as c:
        assert c.execute("SELECT condition FROM item_instances WHERE id=?", (item,)).fetchone()[0] == 40
        assert c.execute("SELECT COUNT(*) FROM world_incidents WHERE world_id=?", (e.wid,)).fetchone()[0] == 1


def test_future_incident_cannot_damage_present_state(crisis):
    from world_engine.incidents import IncidentService

    e, _, _, item, _ = crisis
    with e.db.write() as c:
        site = IncidentService.view(c, e.wid)[0]["sites"][0]
        c.execute("UPDATE world_incidents SET status='resolved' WHERE world_id=?", (e.wid,))
        with pytest.raises(ValueError, match="当前"):
            IncidentService.create(c, e.wid, "未来灰幕", "未来事件尚未发生", [{
                "location_id": site["location_id"], "custodian_id": site["custodian_id"],
                "item_id": item, "recipe_id": site["recipe_id"],
                "resource_keys": site["resource_keys"], "damaged_condition": 20,
                "required_condition": 80,
            }], e.at + timedelta(days=1), idempotency_key="future")
        assert c.execute("SELECT condition FROM item_instances WHERE id=?", (item,)).fetchone()[0] == 40


def test_incident_requests_need_actual_contact_not_ambient_rumor(crisis):
    from world_engine.incidents import IncidentService
    from world_engine.society import SocietyService

    e, _, helper, _, _ = crisis
    with e.db.write() as c:
        IncidentService.tick(c, e.wid, e.at)
        NpcAgencyService.seed(c, e.wid, c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone(), e.at)
        request = IncidentService.view(c, e.wid)[0]["sites"][0]["request_event_id"]
        SocietyService.spread_public_rumors(c, e.wid, e.at + timedelta(days=5))
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                             (helper, request)).fetchone()


def test_multimaterial_tool_and_second_repair_round_progress(crisis):
    from world_engine.incidents import IncidentService

    e, _, helper, _, material = crisis
    with e.db.write() as c:
        site = IncidentService.view(c, e.wid)[0]["sites"][0]
        second, tool = str(uuid4()), str(uuid4())
        for iid, name, category in ((second, "防灰密封料", "material"), (tool, "滤网扳手", "tool")):
            c.execute("INSERT INTO item_types(id,name,category) VALUES (?,?,?)", (iid, name, category))
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                      "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,?,100)",
                      (str(uuid4()), e.wid, iid, helper, helper, 2 if iid == second else 1))
        c.execute("UPDATE item_instances SET quantity=2 WHERE container_id=? AND item_type_id=?", (helper, material))
        spec = json.loads(c.execute("SELECT spec_json FROM activity_recipes WHERE id=?", (site["recipe_id"],)).fetchone()[0])
        spec["ingredients"].append({"item_type_id": second, "quantity": 1})
        spec["tools"] = [{"item_type_id": tool, "wear": 1}]
        spec["repair_points"] = 20
        c.execute("UPDATE activity_recipes SET spec_json=? WHERE id=?", (json.dumps(spec), site["recipe_id"]))
    for _ in range(96):
        e.clock.heartbeat(e.wid, elapsed_seconds=3600)
        with e.db.read() as c:
            result = IncidentService.view(c, e.wid)[0]
            if result["status"] == "resolved":
                break
    with e.db.read() as c:
        details = {"goals": [dict(r) for r in c.execute("SELECT kind,status,next_step_json FROM npc_agency_goals")],
                   "steps": [tuple(r) for r in c.execute("SELECT action_type,status,reason FROM npc_agency_steps")],
                   "items": [tuple(r) for r in c.execute("SELECT item_type_id,container_id,container_type,condition,quantity FROM item_instances")]}
    assert result["status"] == "resolved", json.dumps(details, ensure_ascii=False)
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM character_life_activities WHERE kind='repair' "
                         "AND status='completed'").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM npc_agency_steps WHERE action_type='transfer' "
                         "AND status='accepted'").fetchone()[0] >= 5


def test_global_completion_rechecks_earlier_restored_site(database, settings):
    from scripts.demo_global_incident import build_scenario
    from world_engine.clock import WorldClockService
    from world_engine.engine import WorldEngine
    from world_engine.incidents import IncidentService

    wid, _, at = build_scenario(database)
    with database.write() as c:
        # 哥哥，暂时隔离一位维护人，局部恢复窗口就不会依赖随机 ID 与行动顺序。
        delayed = IncidentService.view(c, wid)[0]["sites"][0]["custodian_id"]
        c.execute("UPDATE characters SET health=0 WHERE id=?", (delayed,))
        c.execute("UPDATE knowledge_evidence SET valid_until=? WHERE edge_id IN "
                  "(SELECT id FROM observer_knowledge_edges WHERE observer_id=? AND predicate='location')",
                  (to_iso(at + timedelta(days=10)), delayed))
    clock, engine = WorldClockService(database, settings), WorldEngine(database, settings)
    for _ in range(72):
        heartbeat = clock.heartbeat(wid, elapsed_seconds=3600)
        if heartbeat.adjudication_due:
            engine.adjudicate(wid, trigger="scheduled_12h")
        with database.read() as c:
            view = IncidentService.view(c, wid)[0]
        if 0 < view["restored_sites"] < 3:
            break
    assert 0 < view["restored_sites"] < 3
    restored = next(s for s in view["sites"] if s["resolution_event_id"])
    with database.write() as c:
        c.execute("UPDATE characters SET health=100 WHERE id=?", (delayed,))
        c.execute("UPDATE item_instances SET condition=40 WHERE id=?", (restored["item_id"],))
        IncidentService.tick(c, wid, heartbeat.current_time)
        changed = IncidentService.view(c, wid)[0]
        assert not next(s for s in changed["sites"] if s["id"] == restored["id"])["resolution_event_id"]
        assert IncidentService.productive_days(c, wid, restored["location_id"], "field_food", heartbeat.current_time, 1) == 0
    for _ in range(10):
        heartbeat = clock.heartbeat(wid, elapsed_seconds=3600)
        if heartbeat.adjudication_due:
            engine.adjudicate(wid, trigger="scheduled_12h")
    with database.read() as c:
        assert IncidentService.view(c, wid)[0]["status"] == "active"
    with database.write() as c:
        helper = c.execute("SELECT subject_id FROM observer_knowledge_edges WHERE observer_id=? "
                           "AND predicate='location' LIMIT 1", (restored["custodian_id"],)).fetchone()[0]
        spec = json.loads(c.execute("SELECT spec_json FROM activity_recipes WHERE id=?",
                                   (restored["recipe_id"],)).fetchone()[0])
        for material in spec["ingredients"]:
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                      "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,?,100)",
                      (str(uuid4()), wid, material["item_type_id"], helper, helper, material["quantity"]))
        colleague = c.execute("SELECT current_location_id FROM characters WHERE id=?", (helper,)).fetchone()[0]
        address = record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=heartbeat.current_time,
                               event_type="world.test_address_known", actor_id=restored["custodian_id"],
                               summary="新到货名单记录同事所在地点")
        KnowledgeService.add_evidence(c, observer_id=restored["custodian_id"], subject_id=helper,
                                     predicate="location", value=colleague, event_id=address, method="document",
                                     quote="同事已收到补给，仍在此处")
    for _ in range(96):
        heartbeat = clock.heartbeat(wid, elapsed_seconds=3600)
        if heartbeat.adjudication_due:
            engine.adjudicate(wid, trigger="scheduled_12h")
        with database.read() as c:
            final = IncidentService.view(c, wid)[0]
        if final["status"] == "resolved":
            break
    with database.read() as c:
        diagnostic = {"incident": final, "goals": [dict(r) for r in c.execute(
            "SELECT character_id,kind,status,predicate_json,next_step_json FROM npc_agency_goals WHERE world_id=?", (wid,))],
            "items": [dict(r) for r in c.execute("SELECT * FROM item_instances WHERE world_id=?", (wid,))]}
    assert final["status"] == "resolved", json.dumps(diagnostic, ensure_ascii=False, indent=2)
    recovered = next(s for s in final["sites"] if s["id"] == restored["id"])
    assert recovered["observation_event_id"] != restored["observation_event_id"]
    assert recovered["resolution_event_id"] != restored["resolution_event_id"]
    with database.read() as c:
        assert c.execute("SELECT COUNT(*) FROM character_life_activities WHERE character_id=? "
                         "AND kind='repair' AND status='completed'", (restored["custodian_id"],)).fetchone()[0] == 2


@pytest.mark.parametrize("damage_while_away", [False, True])
def test_restored_original_can_return_without_unnecessary_new_repair(database, settings, damage_while_away):
    from scripts.demo_global_incident import build_scenario
    from world_engine.clock import WorldClockService
    from world_engine.engine import WorldEngine
    from world_engine.incidents import IncidentService

    wid, _, _ = build_scenario(database)
    with database.write() as c:
        # 哥哥，保留一处尚未参与的地点，原件返回测试就有稳定的局部恢复窗口。
        delayed = IncidentService.view(c, wid)[0]["sites"][0]["custodian_id"]
        c.execute("UPDATE characters SET health=0 WHERE id=?", (delayed,))
    clock, engine = WorldClockService(database, settings), WorldEngine(database, settings)
    for _ in range(72):
        heartbeat = clock.heartbeat(wid, elapsed_seconds=3600)
        if heartbeat.adjudication_due:
            engine.adjudicate(wid, trigger="scheduled_12h")
        with database.read() as c:
            view = IncidentService.view(c, wid)[0]
        if 0 < view["restored_sites"] < 3:
            break
    assert 0 < view["restored_sites"] < 3
    site = next(s for s in view["sites"] if s["resolution_event_id"])
    with database.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (site["custodian_id"],)).fetchone()
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (actor["id"],))
        IncidentService.tick(c, wid, heartbeat.current_time)
        assert not next(s for s in IncidentService.view(c, wid)[0]["sites"]
                        if s["id"] == site["id"])["resolution_event_id"]
        if damage_while_away:
            c.execute("UPDATE item_instances SET condition=40 WHERE id=?", (site["item_id"],))
        c.execute("UPDATE characters SET longitude=? WHERE id=?", (actor["longitude"], actor["id"]))
        IncidentService.tick(c, wid, heartbeat.current_time + timedelta(minutes=1))
        returned = next(s for s in IncidentService.view(c, wid)[0]["sites"] if s["id"] == site["id"])
        if damage_while_away:
            assert not returned["resolution_event_id"]
            assert returned["observation_event_id"] != site["observation_event_id"]
            NpcAgencyService.seed(c, wid, actor, heartbeat.current_time + timedelta(minutes=1))
            assert c.execute("SELECT 1 FROM npc_agency_goals WHERE character_id=? AND kind='incident_repair' "
                             "AND source_event_id=? AND status='active'",
                             (actor["id"], returned["observation_event_id"])).fetchone()
        else:
            assert returned["resolution_event_id"]


def test_worn_tool_requests_and_delivers_a_usable_replacement(crisis):
    from world_engine.incidents import IncidentService

    e, _, helper, _, _ = crisis
    tool = str(uuid4())
    with e.db.write() as c:
        site = IncidentService.view(c, e.wid)[0]["sites"][0]
        c.execute("INSERT INTO item_types(id,name,category) VALUES (?,?,'tool')", (tool, "滤网扳手"))
        for owner, condition in ((e.npc, 1), (helper, 1), (helper, 100)):
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                      "owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,1,?)",
                      (str(uuid4()), e.wid, tool, owner, owner, condition))
        spec = json.loads(c.execute("SELECT spec_json FROM activity_recipes WHERE id=?", (site["recipe_id"],)).fetchone()[0])
        spec["tools"] = [{"item_type_id": tool, "wear": 10}]
        c.execute("UPDATE activity_recipes SET spec_json=? WHERE id=?", (json.dumps(spec), site["recipe_id"]))
        IncidentService.tick(c, e.wid, e.at)
        NpcAgencyService.seed(c, e.wid, c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone(), e.at)
        request = IncidentService.view(c, e.wid)[0]["sites"][0]["request_event_id"]
        supplies = json.loads(c.execute("SELECT payload_json FROM world_events WHERE id=?", (request,)).fetchone()[0])["supplies"]
        assert any(s["item_type_id"] == tool and s["minimum_condition"] == 10 for s in supplies)
    for _ in range(96):
        e.clock.heartbeat(e.wid, elapsed_seconds=3600)
        with e.db.read() as c:
            view = IncidentService.view(c, e.wid)[0]
        if view["status"] == "resolved":
            break
    assert view["status"] == "resolved"
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM item_instances WHERE item_type_id=? AND container_id=? "
                         "AND condition=1", (tool, helper)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM item_instances WHERE item_type_id=? AND owner_character_id=? "
                         "AND condition=90", (tool, e.npc)).fetchone()[0] == 1
