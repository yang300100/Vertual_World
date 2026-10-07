"""Yuki 验证全部政体的真实合作，哥哥的模型 API 不参与测试。"""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest


def save_evidence(db, report):
    from scripts.demo_global_cooperation import write_summary

    output = Path(db.path).parent
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(output, report, database_name=Path(db.path).name)


def test_catalogue_covers_all_current_polities_and_retained_local_government():
    from world_engine.polities import current_polities

    polities = current_polities()
    assert {p["key"] for p in polities} == {f"state-{i}" for i in range(1, 21)} | {"adren-local"}
    assert len({p["capital_key"] for p in polities}) == 21
    assert all(p["name"] and p["source"] for p in polities)


@pytest.fixture
def cooperative(database, settings):
    from scripts.demo_global_cooperation import build_scenario

    settings = replace(settings, decision_provider="rules", world_agent_enabled=False, active_character_limit=100)
    return database, settings, build_scenario(database)


def test_transit_item_keeps_identity_even_when_recipient_has_same_stock(cooperative):
    from world_engine.cooperation import CooperationService as C
    from world_engine.inventory import InventoryService

    db, _, (wid, cid, _) = cooperative
    with db.write() as c:
        view = C.view(c, cid)
        actor = view["members"][0]["local_actor_id"]
        for iid, container, holder in (("existing", "character_inventory", actor), ("cargo", "cooperation_transit", "packet")):
            c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) "
                      "VALUES (?,?,?,?,?,?,1,100)", (iid, wid, view["cure_type_id"], container, holder, actor))
        cargo = c.execute("SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id='cargo'").fetchone()
        InventoryService.transfer(c, cargo, actor, preserve_identity=True)
        assert c.execute("SELECT quantity FROM item_instances WHERE id='existing'").fetchone()[0] == 1
        assert c.execute("SELECT container_id FROM item_instances WHERE id='cargo'").fetchone()[0] == actor
        c.execute("UPDATE characters SET inventory_capacity=4 WHERE id=?", (actor,))
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) "
                  "VALUES ('next-cargo',?,?,'cooperation_transit','next-packet',?,1,100)", (wid, view["cure_type_id"], actor))
        next_cargo = c.execute("SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id='next-cargo'").fetchone()
        with pytest.raises(ValueError, match="容量"):
            InventoryService.transfer(c, next_cargo, actor, preserve_identity=True)
        assert c.execute("SELECT container_type FROM item_instances WHERE id='next-cargo'").fetchone()[0] == "cooperation_transit"


@pytest.mark.parametrize("offset,goal_crisis", [(-24, "current"), (0, "other"), (0, None)])
def test_unrelated_or_pre_crisis_craft_is_not_evidence(cooperative, offset, goal_crisis):
    from world_engine.actions import ActionService
    from world_engine.cooperation import CooperationService as C
    from world_engine.domain import ActionProposal, ActionType
    from world_engine.inventory import InventoryService
    from world_engine.life import LifeActivityService
    from world_engine.npc_agency import NpcAgencyService

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        crisis, _ = C._rows(c, cid)
        recipe = json.loads(crisis["recipes_json"])[0]
        spec = json.loads(c.execute("SELECT spec_json FROM activity_recipes WHERE id=?", (recipe,)).fetchone()[0])
        for ingredient in spec["ingredients"]:
            item = c.execute("SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.item_type_id=?", (ingredient["item_type_id"],)).fetchone()
            InventoryService.transfer(c, item, crisis["coordinator_id"])
        start = at + timedelta(hours=offset)
        metadata = {"recipe_id": recipe}
        if goal_crisis:
            metadata["goal_id"] = NpcAgencyService.adopt(c, wid, crisis["coordinator_id"], "cooperation_craft",
                {"kind": "inventory_at_least", "amount": 1, "item_type_id": spec["output_item_type_id"],
                 "recipe_id": recipe, "crisis_id": cid if goal_crisis == "current" else "other"}, "检验制作来源", start)
        outcome = ActionService().execute(c, world_id=wid, tick_id=str(uuid4()), occurred_at=start,
            proposal=ActionProposal(actor_id=crisis["coordinator_id"], action=ActionType.ACTIVITY, reason="检验真实配方", metadata=metadata))
        assert outcome.accepted, outcome
        active = LifeActivityService.running(c, crisis["coordinator_id"])
        LifeActivityService.finish(c, active, start + timedelta(hours=2), "completed", "达到配方时长")
        assert not C.recipe_finished(c, crisis, recipe)


def test_sample_transfer_for_another_crisis_is_not_contribution(cooperative):
    from world_engine.actions import ActionService
    from world_engine.cooperation import CooperationService as C
    from world_engine.domain import ActionProposal, ActionType
    from world_engine.npc_agency import NpcAgencyService
    from world_engine.repository import to_iso

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        view = C.view(c, cid)
        m = view["members"][0]
        goal = NpcAgencyService.adopt(c, wid, m["delegate_id"], "cooperation_contribute",
            {"kind": "delivered_item", "crisis_id": "another-crisis", "amount": 1,
             "item_type_id": m["sample_type_id"], "target_id": view["coordinator_id"]}, "另一次合作", at)
        outcome = ActionService().execute(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
            proposal=ActionProposal(actor_id=m["delegate_id"], action=ActionType.TRANSFER, target_id=view["coordinator_id"],
                reason="交付另一危机样本", metadata={"item": c.execute("SELECT name FROM item_types WHERE id=?", (m["sample_type_id"],)).fetchone()[0],
                    "item_type_id": m["sample_type_id"], "quantity": 1}))
        assert outcome.accepted, outcome
        c.execute("INSERT INTO npc_agency_steps(id,goal_id,world_time,action_type,status,event_id,expected_json,observed_json,reason) "
                  "VALUES (?,?,?,'transfer','accepted',?,'{}','{}','另一危机')", (str(uuid4()), goal, to_iso(at), outcome.event_id))
        c.execute("UPDATE cooperation_members SET consent_event_id=?,consent_revision=1 WHERE crisis_id=? AND key=?", (outcome.event_id, cid, m["key"]))
        C.tick(c, wid, at)
        assert not C._rows(c, cid, m["key"])[1]["contribution_event_id"]
        c.execute("UPDATE npc_agency_goals SET predicate_json=json_set(predicate_json,'$.crisis_id',?),source_event_id=? WHERE id=?",
                  (cid, outcome.event_id, goal))
        C.tick(c, wid, at)
        assert C._rows(c, cid, m["key"])[1]["contribution_event_id"] == outcome.event_id


def test_report_requires_npc_dispatch_and_travel_time(cooperative):
    from world_engine.cooperation import CooperationService
    from world_engine.npc_agency import NpcAgencyService

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        CooperationService.tick(c, wid, at)
        member = CooperationService.view(c, cid)["members"][0]
        assert member["observation_event_id"]
        assert not member["report_delivery_event_id"]
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
                             (member["delegate_id"], member["observation_event_id"])).fetchone()
        actor = c.execute("SELECT * FROM characters WHERE id=?", (member["local_actor_id"],)).fetchone()
        NpcAgencyService.seed(c, wid, actor, at)
        goals = NpcAgencyService.view(c, actor["id"])
        assert any(g["kind"] == "cooperation_report" for g in goals)
        CooperationService.dispatch(c, actor, cid, member["key"], "report", at)
        packet = c.execute("SELECT * FROM cooperation_packets WHERE crisis_id=?", (cid,)).fetchone()
        assert packet["distance_km"] > 0
        CooperationService.tick(c, wid, at + timedelta(minutes=1))
        assert not CooperationService.view(c, cid)["members"][0]["report_delivery_event_id"]
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (member["delegate_id"],))
        CooperationService.tick(c, wid, at + timedelta(hours=1000))
        assert not CooperationService.view(c, cid)["members"][0]["report_delivery_event_id"]
        c.execute("UPDATE characters SET longitude=longitude-1 WHERE id=?", (member["delegate_id"],))
        CooperationService.tick(c, wid, at + timedelta(hours=1000))
        assert CooperationService.view(c, cid)["members"][0]["report_delivery_event_id"]


def test_revision_and_physical_presence_are_required_even_when_informed(cooperative):
    from world_engine.actions import ActionService
    from world_engine.cooperation import CooperationService as C
    from world_engine.domain import ActionProposal, ActionType

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        C.tick(c, wid, at)
        view = C.view(c, cid)
        m = view["members"][0]
        local = c.execute("SELECT * FROM characters WHERE id=?", (m["local_actor_id"],)).fetchone()
        C.dispatch(c, local, cid, m["key"], "report", at)
        at += timedelta(hours=1000)
        C.tick(c, wid, at)
        outcome = ActionService().execute(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at,
            proposal=ActionProposal(actor_id=m["delegate_id"], action=ActionType.SOCIALIZE, target_id=view["coordinator_id"],
                reason="现场讨论报告", dialogue="本国粮场受灾，需要一起研究。", metadata={"share_event_id": m["observation_event_id"]}))
        assert outcome.accepted
        coordinator = c.execute("SELECT * FROM characters WHERE id=?", (view["coordinator_id"],)).fetchone()
        C.propose(c, coordinator, cid, at)
        delegate = c.execute("SELECT * FROM characters WHERE id=?", (m["delegate_id"],)).fetchone()
        rejected = C.ratify(c, delegate, cid, m["key"], at, revision=1)
        assert c.execute("SELECT event_type FROM world_events WHERE id=?", (rejected,)).fetchone()[0] == "cooperation.policy_rejected"
        C.propose(c, coordinator, cid, at)
        with pytest.raises(ValueError, match="报告"):
            C.ratify(c, delegate, cid, m["key"], at - timedelta(hours=1), revision=2)
        with pytest.raises(ValueError, match="旧版"):
            C.ratify(c, delegate, cid, m["key"], at, revision=1)
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (delegate["id"],))
        distant = c.execute("SELECT * FROM characters WHERE id=?", (delegate["id"],)).fetchone()
        with pytest.raises(ValueError, match="在场"):
            C.ratify(c, distant, cid, m["key"], at, revision=2)
        assert not C._rows(c, cid, m["key"])[1]["consent_event_id"]
        c.execute("UPDATE characters SET longitude=longitude-1 WHERE id=?", (delegate["id"],))
        delegate = c.execute("SELECT * FROM characters WHERE id=?", (delegate["id"],)).fetchone()
        consent = C.ratify(c, delegate, cid, m["key"], at, revision=2)
        assert C._rows(c, cid, m["key"])[1]["consent_event_id"] == consent


def test_injected_cure_stock_cannot_replace_actual_crafting(cooperative):
    from world_engine.cooperation import CooperationService as C

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        view = C.view(c, cid)
        actor = c.execute("SELECT * FROM characters WHERE id=?", (view["coordinator_id"],)).fetchone()
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) "
                  "VALUES ('fake',?,?,'character_inventory',?,?,21,100)", (wid, view["cure_type_id"], actor["id"], actor["id"]))
        c.execute("UPDATE cooperative_crises SET cure_origin_item_id='fake' WHERE id=?", (cid,))
        with pytest.raises(ValueError, match="制作"):
            C.dispatch(c, actor, cid, view["members"][0]["key"], "remedy", at)
        assert not c.execute("SELECT 1 FROM cooperation_packets WHERE crisis_id=?", (cid,)).fetchone()
        assert c.execute("SELECT quantity FROM item_instances WHERE id='fake'").fetchone()[0] == 21


@pytest.mark.parametrize("invalid", ["missing_polity", "coordinator", "recipe"])
def test_crisis_registration_rejects_incomplete_or_invalid_world_references(cooperative, invalid):
    from world_engine.cooperation import CooperationService as C

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        view = C.view(c, cid)
        members = [{**m, "policy": json.loads(m["policy_json"])} for m in view["members"]]
        coordinator, recipes = view["coordinator_id"], json.loads(view["recipes_json"])
        if invalid == "missing_polity":
            members.pop()
        elif invalid == "coordinator":
            coordinator = "unknown-character"
        else:
            recipes[0] = "unknown-recipe"
        before = c.execute("SELECT COUNT(*) FROM world_events WHERE world_id=?", (wid,)).fetchone()[0]
        with pytest.raises(ValueError):
            C.create(c, wid, coordinator, view["hub_id"], members, recipes, view["cure_type_id"], at)
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE world_id=?", (wid,)).fetchone()[0] == before


def test_uninformed_and_unauthorized_delegate_cannot_ratify(cooperative):
    from world_engine.cooperation import CooperationService

    db, _, (wid, cid, at) = cooperative
    with db.write() as c:
        member = CooperationService.view(c, cid)["members"][0]
        actor = c.execute("SELECT * FROM characters WHERE id=?", (member["delegate_id"],)).fetchone()
        with pytest.raises(ValueError):
            CooperationService.ratify(c, actor, cid, member["key"], at)
        stranger = c.execute("SELECT * FROM characters WHERE id=?", (member["local_actor_id"],)).fetchone()
        with pytest.raises(ValueError):
            CooperationService.ratify(c, stranger, cid, member["key"], at)
        assert not CooperationService.view(c, cid)["members"][0]["consent_event_id"]


def test_all_polities_actually_cooperate_and_each_is_necessary(cooperative, monkeypatch):
    from scripts.demo_global_cooperation import simulate
    from world_engine.cooperation import CooperationService

    def forbid_network(*args, **kwargs):
        pytest.fail("哥哥的模型 API 不能用于离线全球协作验收")

    monkeypatch.setattr("httpx.Client.send", forbid_network)
    monkeypatch.setattr("httpx.AsyncClient.send", forbid_network)
    db, settings, (wid, cid, at) = cooperative
    report = simulate(db, settings, wid, cid, at)
    save_evidence(db, report)
    assert report["status"] == "resolved", json.dumps(report["diagnostic"], ensure_ascii=False)
    assert len(report["members"]) == 21
    assert report["model_api_calls"] == 0
    assert report["proposal_revisions"] >= 2
    assert report["policy_rejections"] >= 1
    assert all(m["consent_event_id"] and m["contribution_event_id"] and m["application_event_id"]
               and m["report_delivery_event_id"] for m in report["members"])
    assert len(report["craft_activities"]) == 4
    assert all(a["status"] == "completed" for a in report["craft_activities"])
    assert len(report["resource_recoveries"]) == 21
    assert len({e["location_id"] for e in report["regeneration_events"]}) == 21
    assert len(report["packets"]) == 42
    assert all(p["delivery_event_id"] and p["delivered_world_time"] >= p["arrival_world_time"] for p in report["packets"])
    with db.write() as c:
        assert CooperationService.ready(c, cid)
        for member in report["members"]:
            for field in ("consent_event_id", "contribution_event_id", "application_event_id"):
                c.execute(f"UPDATE cooperation_members SET {field}=NULL WHERE crisis_id=? AND key=?",
                          (cid, member["key"]))
                assert not CooperationService.ready(c, cid), (member["name"], field)
                c.execute(f"UPDATE cooperation_members SET {field}=? WHERE crisis_id=? AND key=?",
                          (member[field], cid, member["key"]))
        member = report["members"][0]
        actor = c.execute("SELECT * FROM characters WHERE id=?", (member["local_actor_id"],)).fetchone()
        before = c.execute("SELECT COUNT(*) FROM inventory_changes WHERE world_id=?", (wid,)).fetchone()[0]
        with pytest.raises(ValueError, match="重复"):
            CooperationService.apply(c, actor, cid, member["key"], at + timedelta(hours=report["world_hours"]))
        assert c.execute("SELECT COUNT(*) FROM inventory_changes WHERE world_id=?", (wid,)).fetchone()[0] == before


def test_a_nonparticipating_polity_prevents_global_resolution(database, settings, monkeypatch):
    from scripts.demo_global_cooperation import build_scenario, simulate

    def forbid_network(*args, **kwargs):
        pytest.fail("反事实验收也不能调用模型 API")

    monkeypatch.setattr("httpx.Client.send", forbid_network)
    monkeypatch.setattr("httpx.AsyncClient.send", forbid_network)
    wid, cid, at = build_scenario(database, withhold="state-13")
    settings = replace(settings, decision_provider="rules", world_agent_enabled=False, active_character_limit=100)
    report = simulate(database, settings, wid, cid, at, hours=1200)
    save_evidence(database, report)
    assert report["status"] == "active"
    missing = next(m for m in report["members"] if m["key"] == "state-13")
    assert not missing["consent_event_id"] and not missing["application_event_id"]
    assert not report["craft_activities"]
    assert report["model_api_calls"] == 0
