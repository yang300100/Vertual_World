"""哥哥，恢复后的真实时钟、租约、重启和失败回滚分别留证。"""
import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest

from test_rides import ride_world  # noqa: F401
from test_rides import boarded, choose, advance, arrival_seconds, current, get, members
from test_ride_runtime import rental
from test_ride_recovery import blocked, propose, accept_recovery, recovery, physical
from world_engine.clock import WorldClockService
from world_engine.contracts import ContractService
from world_engine.database import Database
from world_engine.geo import great_circle_distance_km
from world_engine.movement import MovementService
from world_engine.repository import to_iso, from_iso, utc_now
from world_engine.rides import RideService
from world_engine.ride_recovery import RideRecoveryService


def test_actual_clock_finishes_real_remaining_route_after_pause_beyond_old_deadline(ride_world):
    e=ride_world;rid=blocked(e)
    before=physical(e,rid)
    e.clock.heartbeat(e.wid,elapsed_seconds=3*3600)
    after_pause=physical(e,rid)
    assert after_pause['move']['distance_travelled_km']==before['move']['distance_travelled_km']
    recid=propose(e,rid);accept_recovery(e,rid,recid)
    choose(e,e.player,'resume',rid,recovery_id=recid)
    remaining=arrival_seconds(e,rid)
    e.clock.heartbeat(e.wid,elapsed_seconds=remaining/2)
    assert get(e,rid)['status']=='moving'
    e.clock.heartbeat(e.wid,elapsed_seconds=arrival_seconds(e,rid)+1)
    assert get(e,rid)['status']=='arrived'
    with e.db.read() as c:
        state=c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?',(e.vehicle,)).fetchone()
        assert state['distance_travelled_km']==pytest.approx(before['move']['total_distance_km'],abs=0.002)
        for cid in [e.player,e.npc]:
            person=c.execute('SELECT * FROM characters WHERE id=?',(cid,)).fetchone()
            assert (person['longitude'],person['latitude'])==(state['longitude'],state['latitude'])
            assert RideService.aboard(c,cid)
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?',(e.vehicle,)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM world_heartbeats WHERE world_id=?',(e.wid,)).fetchone()[0]>=3


def test_variable_edge_speeds_use_remaining_seconds_and_do_not_replan(ride_world,monkeypatch):
    e=ride_world
    with e.db.read() as c:
        origin=c.execute('SELECT * FROM locations WHERE id=?',(e.origin,)).fetchone()
        target=c.execute('SELECT * FROM locations WHERE id=?',(e.destination,)).fetchone()
        x,y=origin['longitude'],origin['latitude'];end=target['longitude']
    rid=boarded(e);choose(e,e.player,'depart',rid)
    distance=great_circle_distance_km(x,y,end,y)
    route={'polyline':[[x,y],[(x+end)/2,y],[end,y]],'edges':[{'length_km':distance/2,'speed_kmh':2},{'length_km':distance/2,'speed_kmh':10}]}
    # 哥哥，这是隔离地形规划器给出的明确分段初始路线，之后恢复绝不重规划或补位置。
    with e.db.write() as c:
        at=current(e,c)
        c.execute('UPDATE character_movements SET route_json=?,estimated_arrival_world=? WHERE id=?',(json.dumps(route),to_iso(at+timedelta(hours=distance/4+distance/20)),get(e,rid)['movement_id']))
    advance(e,distance/2/2*3600+distance/4/10*3600)
    with e.db.write() as c:RideService.suspend_vehicle(c,e.vehicle,current(e,c),'隔离规则：分段路线途中挂起')
    recid=propose(e,rid);terms=json.loads(recovery(e,recid)['terms_json'])
    assert terms['remaining_seconds']==pytest.approx(distance/4/10*3600)
    before=physical(e,rid)
    accept_recovery(e,rid,recid)
    monkeypatch.setattr(MovementService,'_plan_route',lambda *a,**k: (_ for _ in ()).throw(AssertionError('恢复不能重规划')))
    choose(e,e.player,'resume',rid,recovery_id=recid)
    assert physical(e,rid)['move']['route_json']==before['move']['route_json']
    advance(e,arrival_seconds(e,rid));assert get(e,rid)['status']=='arrived'


def test_real_lease_expiry_requires_a_new_paid_lease_then_each_new_choice(ride_world):
    e=ride_world;request=rental(e,seconds=3600)
    rid=boarded(e);choose(e,e.player,'depart',rid)
    advance(e,arrival_seconds(e,rid)/2)
    with e.db.write() as c:
        contract=c.execute('SELECT * FROM contract_fulfillments WHERE request_id=?',(request,)).fetchone()
        ContractService.cancel(c,contract,to_iso(current(e,c)))
    assert get(e,rid)['status']=='blocked'
    with pytest.raises(ValueError,match='驾驶使用权'):
        propose(e,rid)
    with e.db.write() as c:
        request_id=str(uuid4());at=current(e,c)
        terms={'vehicle_id':e.vehicle,'payment':5,'duration_days':1}
        before=c.execute('SELECT money FROM characters WHERE id=?',(e.player,)).fetchone()[0]
        c.execute("INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,terms_json,status,created_at,updated_at) VALUES (?,?,?,?,'lease',?,'npc_accepted',?,?)",(request_id,e.wid,e.player,e.npc,json.dumps(terms),to_iso(utc_now()),to_iso(utc_now())))
        new=c.execute('SELECT * FROM long_term_operation_requests WHERE id=?',(request_id,)).fetchone()
        ContractService.start(c,new,terms,to_iso(at))
        assert c.execute('SELECT money FROM characters WHERE id=?',(e.player,)).fetchone()[0]==before-5
    recid=propose(e,rid);accept_recovery(e,rid,recid);choose(e,e.player,'resume',rid,recovery_id=recid)
    assert get(e,rid)['status']=='moving'


@pytest.mark.parametrize('condition',['driver','vehicle','lease','authorization'])
def test_changed_condition_after_resume_blocks_before_any_additional_distance(ride_world,condition):
    e=ride_world
    if condition=='lease':request=rental(e,seconds=3600)
    rid=blocked(e);recid=propose(e,rid);accept_recovery(e,rid,recid);choose(e,e.player,'resume',rid,recovery_id=recid)
    with e.db.write() as c:
        if condition=='driver':c.execute('UPDATE characters SET satiety=30 WHERE id=?',(e.player,))
        elif condition=='vehicle':c.execute('UPDATE vehicles SET is_available=0 WHERE id=?',(e.vehicle,))
        elif condition=='lease':
            contract=c.execute('SELECT * FROM contract_fulfillments WHERE request_id=?',(request,)).fetchone();ContractService.cancel(c,contract,to_iso(current(e,c)))
        else:c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?",(recovery(e,recid)['execution_event_id'],))
    before=physical(e,rid);advance(e,10)
    assert get(e,rid)['status']=='blocked'
    after=physical(e,rid)
    assert after['move']['distance_travelled_km']==before['move']['distance_travelled_km']
    assert after['people']==before['people']


def test_failed_resume_rolls_back_movement_event_and_every_authorization(ride_world):
    e=ride_world;rid=blocked(e);recid=propose(e,rid);accept_recovery(e,rid,recid)
    with e.db.write() as c:
        c.execute("CREATE TRIGGER fail_recovery_after_movement BEFORE UPDATE OF status ON vehicle_states WHEN NEW.status='moving' BEGIN SELECT RAISE(ABORT,'隔离回滚哨兵'); END")
    before=physical(e,rid);original=recovery(e,recid)
    with e.db.read() as c:count=c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError,match='回滚哨兵'):
        choose(e,e.player,'resume',rid,recovery_id=recid)
    assert physical(e,rid)==before and recovery(e,recid)==original
    with e.db.read() as c:assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]==count


def test_new_recovery_deadline_expiry_never_uses_old_accepted_sources(ride_world):
    e=ride_world;rid=blocked(e);recid=propose(e,rid,seconds=400);accept_recovery(e,rid,recid)
    advance(e,401)
    before=physical(e,rid)
    with pytest.raises(ValueError):choose(e,e.player,'resume',rid,recovery_id=recid)
    assert recovery(e,recid)['status']=='expired' and physical(e,rid)==before


def test_partial_original_boarding_cannot_be_relabelled_as_recoverable_journey(ride_world):
    e=ride_world;rid=boarded(e)
    with e.db.write() as c:RideService.suspend_vehicle(c,e.vehicle,current(e,c),'原站还未出发')
    with pytest.raises(ValueError,match='途中|路线'):
        propose(e,rid)


def test_three_person_recovery_requires_every_individual_and_no_one_can_leave_silently(ride_world):
    e=ride_world;rid=blocked(e,party=[e.player,e.npc,e.other]);recid=propose(e,rid)
    choose(e,e.npc,'accept_recovery',rid,recovery_id=recid)
    with pytest.raises(ValueError,match='全员'):choose(e,e.player,'resume',rid,recovery_id=recid)
    choose(e,e.other,'decline_recovery',rid,recovery_id=recid)
    assert get(e,rid)['status']=='blocked' and recovery(e,recid)['status']=='declined'
    with e.db.read() as c:assert all(RideService.aboard(c,cid) for cid in [e.player,e.npc,e.other])


def test_disembarked_person_has_no_current_recovery_obligation_while_others_remain(ride_world):
    e=ride_world;rid=blocked(e,party=[e.player,e.npc,e.other]);recid=propose(e,rid)
    accept_recovery(e,rid,recid,party=[e.npc,e.other])
    choose(e,e.player,'resume',rid,recovery_id=recid)
    advance(e,arrival_seconds(e,rid))
    choose(e,e.npc,'disembark',rid)
    with e.db.read() as c:
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e.npc,)).fetchone()
        # 哥哥，别人还在车上不应让已下车的本人继续承担恢复或登离义务。
        assert RideService.aboard(c,e.player) and RideService.aboard(c,e.npc) is None
        assert any(row['id']==rid for row in RideService.rows(c,actor,current(e,c)))
        assert RideService.context(c,actor,current(e,c))==[]
