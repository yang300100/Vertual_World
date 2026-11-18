"""哥哥，只在隔离世界验证 NPC 通过真实路径解决饥饿。"""

import json
from datetime import timedelta

import pytest

from test_lived_world import town  # noqa: F401
from world_engine.actions import ActionService
from world_engine.daily_life import DailyLifeService
from world_engine.domain import ActionProposal, ActionType
from world_engine.economy import EconomyService
from world_engine.npc_operators import _local_food_options
from world_engine.movement import MovementService, RouteUnavailable
from world_engine.routing import RoutePlan, RoutePlanner
from world_engine.repository import from_iso, to_iso


def prepare_hungry(e, *, resource_owner=None, known_food=False):
    """哥哥，以下仅设置隔离夹具的初始状态，后续时间与位置由真实心跳推进。"""
    with e.db.write() as c:
        c.execute("DELETE FROM item_instances WHERE container_type='character_inventory' AND container_id=?", (e.npc,))
        if known_food:
            # 哥哥，本人曾在现场真实采集并食用，给后续寻食留下可核验经历。
            c.execute("UPDATE characters SET energy=90,satiety=20 WHERE id=?", (e.npc,))
            actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
            assert EconomyService.harvest(c, actor, e.at, food_only=True)
            outcome = ActionService().execute(c, world_id=e.wid, tick_id="food-source-observation",
                occurred_at=e.at, proposal=ActionProposal(actor_id=e.npc, action=ActionType.EAT,
                                                         reason="本人食用真实采集的食物"))
            assert outcome.accepted
        c.execute("UPDATE characters SET longitude=?,latitude=?,energy=90,satiety=20 WHERE id=?",
                  (e.loc['longitude'] + 0.002, e.loc['latitude'], e.npc))
        c.execute("UPDATE world_clock SET next_adjudication_world_time=? WHERE world_id=?", (to_iso(e.at), e.wid))
        c.execute("UPDATE world_item_profiles SET last_growth_world_time=?,resource_owner_id=? WHERE world_id=? AND resource_key='food' AND resource_location_id IS NULL",
                  (to_iso(e.at), resource_owner, e.wid))


def test_distant_actual_food_is_not_mistaken_for_immediate_harvest(town):
    e = town
    prepare_hungry(e)
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        place = c.execute("SELECT * FROM locations WHERE id=?", (e.loc['id'],)).fetchone()
        assert json.loads(place['resources_json'])['food'] > 0
        before = c.total_changes
        _, can_harvest = _local_food_options(c, npc, e.at)
        assert c.total_changes == before and not can_harvest
        assert not EconomyService.harvest(c, npc, e.at, food_only=True)


def test_due_adjudication_does_not_prevent_real_food_recovery(town):
    e = town
    prepare_hungry(e, known_food=True)
    with e.db.write() as c:
        before = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        previous = c.execute("SELECT COUNT(*) FROM world_events WHERE actor_id=? AND event_type='action.resource_harvested'", (e.npc,)).fetchone()[0]
        DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
        movement = c.execute("SELECT * FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert movement and movement['destination_longitude'] == e.loc['longitude']
        assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0]) == before
        arrival = from_iso(movement['estimated_arrival_world'])
    e.clock.heartbeat(e.wid, elapsed_seconds=(arrival - e.at).total_seconds() + 1)
    # 哥哥，日常调度下一次检查是一个世界小时，不凭测试强推短时间再进食。
    e.clock.heartbeat(e.wid, elapsed_seconds=3601)
    with e.db.read() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        after = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        assert actor['satiety'] > 20
        events = c.execute("SELECT payload_json FROM world_events WHERE actor_id=? AND event_type='action.resource_harvested'", (e.npc,)).fetchall()
        assert len(events) == previous + 1
        item_id = json.loads(events[-1]['payload_json'])['item_type_id']
        key = c.execute("SELECT resource_key FROM world_item_profiles WHERE world_id=? AND item_type_id=?", (e.wid, item_id)).fetchone()[0]
        assert after[key] == before[key] - 1
        assert {k: v for k, v in after.items() if k != key} == {k: v for k, v in before.items() if k != key}
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE actor_id=? AND event_type='action.eat'", (e.npc,)).fetchone()[0] >= 1


def test_known_past_food_does_not_reveal_remote_current_stock(town):
    e = town
    prepare_hungry(e, known_food=True)
    with e.db.write() as c:
        original = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        outcomes = []
        for stock in (0, 20):
            c.execute("SAVEPOINT remote_food_case")
            resources = {key: (stock if key in {"food", "bakery_supply"} else value) for key, value in original.items()}
            c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps(resources), e.loc['id']))
            DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
            movement = c.execute("SELECT destination_longitude,destination_latitude FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
            outcomes.append(tuple(movement) if movement else None)
            c.execute("ROLLBACK TO remote_food_case")
            c.execute("RELEASE remote_food_case")
        assert outcomes == [(e.loc['longitude'], e.loc['latitude'])] * 2


def test_future_food_knowledge_does_not_justify_travel(town):
    e = town
    prepare_hungry(e, known_food=True)
    with e.db.write() as c:
        event = c.execute("SELECT id FROM world_events WHERE actor_id=? AND event_type='action.resource_harvested'", (e.npc,)).fetchone()[0]
        future = to_iso(e.at + timedelta(hours=1))
        c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, event))
        c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=?", (future, event))
        DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()


def prepare_familiar_departure(e):
    """哥哥，先让本人从地点中心真实走开，再建立隔离的饥饿测试状态。"""
    with e.db.write() as c:
        movement = MovementService().start(c, world_id=e.wid, character_id=e.npc,
            destination_longitude=e.loc['longitude'] + 0.002, destination_latitude=e.loc['latitude'],
            world_time=e.at)
    e.clock.heartbeat(e.wid, elapsed_seconds=(movement.estimated_arrival_world - e.at).total_seconds() + 1)
    with e.db.write() as c:
        at = from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0] == "arrived"
        c.execute("DELETE FROM item_instances WHERE container_type='character_inventory' AND container_id=?", (e.npc,))
        c.execute("UPDATE characters SET satiety=20,energy=90 WHERE id=?", (e.npc,))
        c.execute("UPDATE world_item_profiles SET last_growth_world_time=? WHERE world_id=? AND resource_key='food'", (to_iso(at), e.wid))
        return at


def test_familiar_place_exploration_does_not_use_remote_stock(town):
    e = town
    at = prepare_familiar_departure(e)
    with e.db.write() as c:
        original = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        outcomes = []
        for stock in (0, 20):
            c.execute("SAVEPOINT familiar_food_case")
            resources = {key: (stock if key in {"food", "bakery_supply"} else value) for key, value in original.items()}
            c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps(resources), e.loc['id']))
            DailyLifeService.tick(c, e.wid, at, adjudication_due=True)
            movement = c.execute("SELECT destination_longitude,destination_latitude FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
            outcomes.append(tuple(movement) if movement else None)
            c.execute("ROLLBACK TO familiar_food_case")
            c.execute("RELEASE familiar_food_case")
        assert outcomes == [(e.loc['longitude'], e.loc['latitude'])] * 2


def test_empty_familiar_place_is_not_repeatedly_revisited(town):
    e = town
    at = prepare_familiar_departure(e)
    with e.db.write() as c:
        resources = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        for key in ("food", "bakery_supply"):
            if key in resources:
                resources[key] = 0
        c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps(resources), e.loc['id']))
        DailyLifeService.tick(c, e.wid, at, adjudication_due=True)
        movement = c.execute("SELECT * FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert movement
        seconds = (from_iso(movement['estimated_arrival_world']) - at).total_seconds() + 1
    e.clock.heartbeat(e.wid, elapsed_seconds=seconds)
    with e.db.read() as c:
        before = c.execute("SELECT COUNT(*) FROM character_movements WHERE character_id=?", (e.npc,)).fetchone()[0]
    for _ in range(3):
        e.clock.heartbeat(e.wid, elapsed_seconds=3601)
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE character_id=?", (e.npc,)).fetchone()[0] == before
        assert not c.execute("SELECT 1 FROM item_instances WHERE container_type='character_inventory' AND container_id=?", (e.npc,)).fetchone()


@pytest.mark.parametrize("fault", ["future_event", "future_knowledge", "missing_knowledge", "wrong_location"])
def test_familiar_exploration_requires_current_personal_movement_source(town, fault):
    e = town
    at = prepare_familiar_departure(e)
    with e.db.write() as c:
        # 哥哥，排除其他人物真实采集时的旁观见闻，只隔离检验这一条移动来源。
        c.execute("DELETE FROM character_event_knowledge WHERE character_id=? AND event_id IN (SELECT id FROM world_events WHERE event_type='action.resource_harvested')", (e.npc,))
        source = c.execute("SELECT id FROM world_events WHERE actor_id=? AND event_type='action.movement_started'", (e.npc,)).fetchone()[0]
        future = to_iso(at + timedelta(hours=1))
        if fault == "future_event":
            c.execute("UPDATE world_events SET occurred_at=? WHERE id=?", (future, source))
        elif fault == "future_knowledge":
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=? AND character_id=?", (future, source, e.npc))
        elif fault == "missing_knowledge":
            c.execute("DELETE FROM character_event_knowledge WHERE event_id=? AND character_id=?", (source, e.npc))
        else:
            c.execute("UPDATE world_events SET location_id=NULL WHERE id=?", (source,))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        locations = {row['id']: row for row in c.execute("SELECT * FROM locations WHERE world_id=?", (e.wid,))}
        assert DailyLifeService.familiar_place_to_search(c, actor, at, locations) is None


def test_no_authority_does_not_create_food_or_travel(town):
    e = town
    prepare_hungry(e, resource_owner=e.player)
    with e.db.write() as c:
        before = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        assert not c.execute("SELECT 1 FROM item_instances WHERE owner_character_id=? AND container_type='character_inventory'", (e.npc,)).fetchone()
        assert json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0]) == before


@pytest.mark.parametrize("kind", ["city", "town"])
def test_person_can_explore_own_settlement_without_knowing_remote_food_stock(town, kind):
    e = town
    prepare_hungry(e)
    with e.db.write() as c:
        c.execute("UPDATE locations SET kind=?,area_radius_km=1 WHERE id=?", (kind, e.loc['id']))
        original = json.loads(c.execute("SELECT resources_json FROM locations WHERE id=?", (e.loc['id'],)).fetchone()[0])
        destinations = []
        for stock in (0, 20):
            c.execute("SAVEPOINT settlement_exploration")
            resources = {key: (stock if key in {"food", "bakery_supply"} else value) for key, value in original.items()}
            c.execute("UPDATE locations SET resources_json=? WHERE id=?", (json.dumps(resources), e.loc['id']))
            DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
            movement = c.execute("SELECT destination_longitude,destination_latitude FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
            destinations.append(tuple(movement) if movement else None)
            c.execute("ROLLBACK TO settlement_exploration")
            c.execute("RELEASE settlement_exploration")
        assert destinations == [(e.loc['longitude'], e.loc['latitude'])] * 2


@pytest.mark.parametrize("fault", ["wrong_layer", "outside_area", "inactive", "foreign_world", "non_settlement"])
def test_unfamiliar_or_invalid_settlement_does_not_authorize_exploration(town, fault):
    e = town
    prepare_hungry(e)
    with e.db.write() as c:
        c.execute("UPDATE locations SET kind='city',area_radius_km=1 WHERE id=?", (e.loc['id'],))
        if fault == "wrong_layer":
            c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (e.npc,))
        elif fault == "outside_area":
            c.execute("UPDATE locations SET area_radius_km=0.01 WHERE id=?", (e.loc['id'],))
        elif fault == "inactive":
            c.execute("UPDATE locations SET is_active=0 WHERE id=?", (e.loc['id'],))
        elif fault == "non_settlement":
            c.execute("UPDATE locations SET kind='workplace' WHERE id=?", (e.loc['id'],))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        place = dict(c.execute("SELECT * FROM locations WHERE id=?", (e.loc['id'],)).fetchone())
        if fault == "foreign_world":
            place["world_id"] = "隔离其他世界"
        assert DailyLifeService.familiar_place_to_search(c, actor, e.at, {place['id']: place}) is None


def _blocked_settlement(e):
    """哥哥，隔离出本人急需食物但需要真实走向城镇的场景。"""
    prepare_hungry(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=0,money=0 WHERE id=?", (e.npc,))
        c.execute("UPDATE locations SET kind='town',area_radius_km=1 WHERE id=?", (e.loc['id'],))
        c.execute("INSERT INTO navigation_datasets(id,world_id,name,asset_root,source_sha256,review_status,bounds_json,created_at) "
                  "VALUES ('test-route',?,'隔离地图','none','test','approved','{}',?)", (e.wid,to_iso(e.at)))


@pytest.mark.parametrize("unavailable", [True, False])
def test_unreachable_familiar_search_preserves_earlier_agency_only(town, monkeypatch, unavailable):
    e = town
    _blocked_settlement(e)

    def failed_plan(self, c, *, origin, destination, movement_type, **kwargs):
        # 哥哥，模拟规划中已发生的临时副作用，必须随本次移动回滚。
        c.execute("UPDATE characters SET energy=1 WHERE id=?", (e.npc,))
        if not unavailable:
            raise ValueError("其他错误仍须回滚整次行动")
        return RoutePlan(False, origin, destination, movement_type, unreachable_reason="河道没有已登记的通行路线")

    monkeypatch.setattr(RoutePlanner, "plan", failed_plan)
    with e.db.write() as c:
        before = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone())
        DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
        assert dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()) == before
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e.npc,)).fetchone()
        state = c.execute("SELECT intention,last_error FROM npc_daily_states WHERE character_id=?", (e.npc,)).fetchone()
        goals = c.execute("SELECT status FROM npc_agency_goals WHERE character_id=?", (e.npc,)).fetchall()
        if unavailable:
            assert "暂时无法前往" in state['intention'] and "河道没有已登记的通行路线" in state['intention']
            assert state['last_error'] is None
            assert goals and all(goal['status'] == 'blocked' for goal in goals)
        else:
            assert state['last_error'] == "其他错误仍须回滚整次行动"
            assert not goals


def test_unavailable_route_is_a_value_error_for_existing_callers(town, monkeypatch):
    e = town
    _blocked_settlement(e)
    monkeypatch.setattr(RoutePlanner, "plan", lambda self, c, *, origin, destination, movement_type, **kwargs:
                        RoutePlan(False, origin, destination, movement_type, unreachable_reason="无桥渡"))
    with e.db.write() as c:
        with pytest.raises(ValueError, match="无桥渡") as error:
            MovementService().start(c, world_id=e.wid, character_id=e.npc,
                destination_longitude=e.loc['longitude'], destination_latitude=e.loc['latitude'], world_time=e.at)
        assert isinstance(error.value, RouteUnavailable)


def test_food_received_after_failed_route_can_be_eaten(town, monkeypatch):
    from uuid import uuid4
    e = town
    _blocked_settlement(e)
    monkeypatch.setattr(RoutePlanner, "plan", lambda self, c, *, origin, destination, movement_type, **kwargs:
                        RoutePlan(False, origin, destination, movement_type, unreachable_reason="无桥渡"))
    with e.db.write() as c:
        DailyLifeService.tick(c, e.wid, e.at, adjudication_due=True)
        assert "暂时无法前往" in c.execute("SELECT intention FROM npc_daily_states WHERE character_id=?", (e.npc,)).fetchone()[0]
        # 哥哥，这份食物只在隔离夹具中注入本人背包，模拟之后获得的真实物品。
        item = c.execute("SELECT p.item_type_id FROM world_item_profiles p WHERE p.world_id=? AND p.nutrition>0 LIMIT 1", (e.wid,)).fetchone()[0]
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) "
                  "VALUES (?,?,?,'character_inventory',?,?,1,100)", (str(uuid4()),e.wid,item,e.npc,e.npc))
        DailyLifeService.tick(c, e.wid, e.at + timedelta(minutes=1), adjudication_due=True)
        assert c.execute("SELECT satiety FROM characters WHERE id=?", (e.npc,)).fetchone()[0] > 0
