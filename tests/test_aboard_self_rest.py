"""哥哥，这些测试只在隔离世界验证本人在真实载具上的自主休息。"""

from datetime import timedelta
from uuid import uuid4
import json
import sqlite3

import pytest

from test_rides import ride_world, boarded, choose, advance, arrival_seconds, current, get  # noqa: F401
from test_ride_recovery import blocked, propose, accept_recovery
from test_ride_handoff import ready, take
from world_engine.actions import ActionService
from world_engine.clock import WorldClockService
from world_engine.database import Database
from world_engine.domain import ActionProposal, ActionType
from world_engine.life import LifeActivityError, LifeActivityService
from world_engine.repository import to_iso
from world_engine.rides import RideService
from world_engine.rides import RideIntent


@pytest.fixture
def rest_world(ride_world):
    with ride_world.db.write() as c:
        LifeActivityService.install_aboard(c)
    return ride_world


def start(e, cid, kind='rest', minutes=12):
    with e.db.write() as c:
        person = c.execute('SELECT * FROM characters WHERE id=?', (cid,)).fetchone()
        return LifeActivityService.start(c, person, current(e, c), kind, minutes)


def activity(e, aid):
    with e.db.read() as c:
        return dict(c.execute('SELECT * FROM character_life_activities WHERE id=?', (aid,)).fetchone())


@pytest.mark.parametrize('kind', ['rest', 'wait'])
def test_blocked_aboard_rest_is_personal_and_uses_real_position(rest_world, kind):
    e=rest_world;rid=blocked(e)
    with e.db.write() as c:
        c.execute('UPDATE characters SET energy=10 WHERE id=?',(e.player,))
        before=dict(c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?',(e.vehicle,)).fetchone())
        money=c.execute('SELECT money FROM characters WHERE id=?',(e.player,)).fetchone()[0]
    aid=start(e,e.player,kind)
    with e.db.read() as c:
        row=dict(c.execute('SELECT * FROM character_life_activities WHERE id=?',(aid,)).fetchone())
        bound=dict(c.execute('SELECT * FROM aboard_life_activities WHERE activity_id=?',(aid,)).fetchone())
        assert row['location_id']==before['current_location_id']
        assert bound['ride_id']==rid and bound['vehicle_id']==e.vehicle
        assert (row['longitude'],row['latitude'])==(before['longitude'],before['latitude'])
        assert c.execute('SELECT COUNT(*) FROM character_life_activities WHERE character_id=? AND status=\'running\'',(e.npc,)).fetchone()[0]==0
    e.clock.heartbeat(e.wid,elapsed_seconds=12*60)
    assert activity(e,aid)['status']=='completed'
    with e.db.read() as c:
        person=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
        assert person['health']==100 and person['money']==money
        assert RideService.aboard(c,e.player)['id']==rid
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?',(e.vehicle,)).fetchone()[0]==1
        if kind=='rest':assert person['energy']>10
@pytest.mark.parametrize('phase',['boarding','moving','arrived'])
def test_other_ride_phases_do_not_allow_aboard_rest(rest_world,phase):
    e=rest_world;rid=boarded(e)
    if phase!='boarding':
        choose(e,e.player,'depart',rid)
    if phase=='arrived':
        advance(e,arrival_seconds(e,rid))
    with pytest.raises(LifeActivityError,match='静止挂起'):
        start(e,e.player)
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM aboard_life_activities').fetchone()[0]==0


@pytest.mark.parametrize('fault',['layer','position','location','vehicle_moving','member_not_live'])
def test_invalid_physical_or_member_state_cannot_start(rest_world,fault):
    e=rest_world;rid=blocked(e)
    with e.db.write() as c:
        if fault=='layer':c.execute("UPDATE characters SET space_layer='sky' WHERE id=?",(e.player,))
        elif fault=='position':c.execute('UPDATE characters SET longitude=longitude+0.002 WHERE id=?',(e.player,))
        elif fault=='location':c.execute('UPDATE vehicle_states SET current_location_id=? WHERE vehicle_id=?',(e.origin,e.vehicle))
        elif fault=='vehicle_moving':c.execute("UPDATE vehicle_states SET status='moving' WHERE vehicle_id=?",(e.vehicle,))
        else:c.execute('UPDATE vehicle_ride_members SET live=0 WHERE ride_id=? AND character_id=?',(rid,e.player))
    with pytest.raises(LifeActivityError):start(e,e.player)
    with e.db.read() as c:assert c.execute('SELECT COUNT(*) FROM aboard_life_activities').fetchone()[0]==0


def test_other_activities_are_not_granted_aboard(rest_world):
    e=rest_world;blocked(e)
    with pytest.raises(LifeActivityError,match='只有'):
        start(e,e.player,'work',10)


@pytest.mark.parametrize('kind',['rest','wait'])
def test_resting_member_blocks_recovery_and_handoff_until_their_activity_ends(rest_world,kind):
    e=rest_world;rid=blocked(e);aid=start(e,e.npc,kind)
    with pytest.raises(ValueError,match='当前活动'):
        propose(e,rid)
    with e.db.read() as c:deadline=current(e,c)+timedelta(hours=1)
    with pytest.raises(ValueError,match='当前活动'):
        choose(e,e.player,'propose_handoff',rid,controller_id=e.npc,deadline_world_time=deadline)
    assert get(e,rid)['status']=='blocked' and activity(e,aid)['status']=='running'
    e.clock.heartbeat(e.wid,elapsed_seconds=12*60)
    assert activity(e,aid)['status']=='completed'
    assert propose(e,rid)


@pytest.mark.parametrize('route',['recovery','handoff'])
@pytest.mark.parametrize('kind',['rest','wait'])
def test_new_rest_after_unanimous_consent_blocks_actual_departure(rest_world,route,kind):
    e=rest_world;rid=blocked(e)
    if route=='recovery':
        choice_id=propose(e,rid);accept_recovery(e,rid,choice_id)
    else:
        choice_id=ready(e,rid)
    aid=start(e,e.npc,kind)
    with pytest.raises(ValueError):
        if route=='recovery':choose(e,e.player,'resume',rid,recovery_id=choice_id)
        else:take(e,rid,choice_id)
    assert get(e,rid)['status']=='blocked' and activity(e,aid)['status']=='running'
    e.clock.heartbeat(e.wid,elapsed_seconds=12*60)
    assert activity(e,aid)['status']=='completed' and get(e,rid)['status']=='blocked'
    if route=='recovery':
        choice_id=propose(e,rid);accept_recovery(e,rid,choice_id)
        choose(e,e.player,'resume',rid,recovery_id=choice_id)
    else:
        choice_id=ready(e,rid);take(e,rid,choice_id)
    assert get(e,rid)['status']=='moving' and activity(e,aid)['status']=='completed'


@pytest.mark.parametrize('fault',['ride_moving','vehicle_moving','vehicle_moved','layer_changed','membership_ended'])
def test_changed_binding_interrupts_without_granting_remaining_rest(rest_world,fault):
    e=rest_world;rid=blocked(e)
    with e.db.write() as c:c.execute('UPDATE characters SET energy=10 WHERE id=?',(e.npc,))
    aid=start(e,e.npc)
    with e.db.write() as c:
        if fault=='ride_moving':c.execute("UPDATE vehicle_rides SET status='moving' WHERE id=?",(rid,))
        elif fault=='vehicle_moving':c.execute("UPDATE vehicle_states SET status='moving' WHERE vehicle_id=?",(e.vehicle,))
        elif fault=='vehicle_moved':c.execute('UPDATE vehicle_states SET longitude=longitude+0.002 WHERE vehicle_id=?',(e.vehicle,))
        elif fault=='layer_changed':c.execute("UPDATE characters SET space_layer='sky' WHERE id=?",(e.npc,))
        else:c.execute('UPDATE vehicle_ride_members SET live=0,disembarked_at=? WHERE ride_id=? AND character_id=?',(to_iso(current(e,c)),rid,e.npc))
        eclock=current(e,c);now=eclock+timedelta(minutes=12)
        LifeActivityService.advance(c,e.wid,now,str(uuid4()))
        assert c.execute('SELECT energy FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==10
    row=activity(e,aid);assert row['status']=='interrupted' and row['finish_event_id']


def test_reopen_preserves_binding_and_real_elapsed_time(rest_world):
    e=rest_world;rid=blocked(e);aid=start(e,e.npc)
    reopened=Database(e.settings.database_path);reopened.initialize()
    with reopened.write() as c:LifeActivityService.install_aboard(c)
    e.db=e.database=reopened;e.clock=WorldClockService(reopened,e.settings)
    e.clock.heartbeat(e.wid,elapsed_seconds=6*60)
    assert activity(e,aid)['status']=='running'
    e.clock.heartbeat(e.wid,elapsed_seconds=6*60)
    assert activity(e,aid)['status']=='completed'
    with reopened.read() as c:assert c.execute('SELECT ride_id FROM aboard_life_activities WHERE activity_id=?',(aid,)).fetchone()[0]==rid


def test_failure_to_create_binding_rolls_back_activity_in_same_transaction(rest_world):
    e=rest_world;blocked(e)
    with e.db.write() as c:
        c.execute("CREATE TRIGGER fail_aboard_binding BEFORE INSERT ON aboard_life_activities BEGIN SELECT RAISE(ABORT,'隔离回滚哨兵'); END")
        before=c.execute('SELECT COUNT(*) FROM character_life_activities').fetchone()[0]
        person=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError,match='回滚哨兵'):
            LifeActivityService.start(c,person,current(e,c),'rest',12)
        assert c.execute('SELECT COUNT(*) FROM character_life_activities').fetchone()[0]==before
        assert c.execute('SELECT COUNT(*) FROM aboard_life_activities').fetchone()[0]==0


def test_action_start_and_completion_keep_no_fabricated_shore_location(rest_world):
    e=rest_world;rid=blocked(e)
    with e.db.write() as c:
        state=c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?',(e.vehicle,)).fetchone()
        assert state['current_location_id'] is None
        result=ActionService().execute(c,world_id=e.wid,tick_id=str(uuid4()),occurred_at=current(e,c),
            proposal=ActionProposal(actor_id=e.player,action=ActionType.REST,reason='我本人在载具内休息。',metadata={'duration_minutes':12}))
        assert result.accepted
        event=c.execute('SELECT * FROM world_events WHERE id=?',(result.event_id,)).fetchone()
        row=c.execute('SELECT * FROM character_life_activities WHERE character_id=? AND status=\'running\'',(e.player,)).fetchone()
        assert event['location_id'] is None and row['location_id'] is None
        assert json.loads(event['payload_json'])['aboard_activity']=={'ride_id':rid,'vehicle_id':e.vehicle}
        aid=row['id']
    e.clock.heartbeat(e.wid,elapsed_seconds=12*60)
    with e.db.read() as c:
        row=c.execute('SELECT * FROM character_life_activities WHERE id=?',(aid,)).fetchone()
        event=c.execute('SELECT * FROM world_events WHERE id=?',(row['finish_event_id'],)).fetchone()
        assert event['location_id'] is None and event['actor_id']==e.player
        assert json.loads(event['payload_json'])['aboard_activity']=={'ride_id':rid,'vehicle_id':e.vehicle}


def test_safe_personal_disembark_interrupts_own_rest_at_the_actual_time(rest_world):
    e=rest_world;rid=boarded(e)
    with e.db.write() as c:
        RideService.suspend_vehicle(c,e.vehicle,current(e,c),'原站的隔离挂起')
        at=to_iso(current(e,c))
    aid=start(e,e.npc)
    choose(e,e.npc,'disembark',rid)
    row=activity(e,aid)
    assert row['status']=='interrupted' and row['finished_world_time']==at and row['finish_event_id']
    with e.db.read() as c:
        assert RideService.aboard(c,e.npc) is None and RideService.aboard(c,e.player)['id']==rid
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?',(e.vehicle,)).fetchone()[0]==0


def test_failed_disembark_rolls_back_interruption_in_same_transaction(rest_world, monkeypatch):
    e=rest_world;rid=boarded(e)
    with e.db.write() as c:
        RideService.suspend_vehicle(c,e.vehicle,current(e,c),'原站的隔离挂起')
    aid=start(e,e.npc)
    original=RideService._event.__func__
    def fail_disembark(cls,c,row,actor,at,operation,**kwargs):
        if operation=='disembarked':
            raise ValueError('隔离下车失败哨兵')
        return original(cls,c,row,actor,at,operation,**kwargs)
    monkeypatch.setattr(RideService,'_event',classmethod(fail_disembark))
    with e.db.write() as c:
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e.npc,)).fetchone()
        events=c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
        with pytest.raises(ValueError,match='隔离下车失败哨兵'):
            RideService.apply(c,actor,current(e,c),RideIntent(operation='disembark',ride_id=rid,statement='本人选择下车。'))
        row=c.execute('SELECT * FROM character_life_activities WHERE id=?',(aid,)).fetchone()
        assert row['status']=='running' and row['finish_event_id'] is None
        assert RideService.aboard(c,e.npc)['id']==rid
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]==events


def test_stale_cross_world_actor_cannot_start_or_write_activity(rest_world):
    e=rest_world;blocked(e)
    with e.db.write() as c:
        person=dict(c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone())
        person['world_id']='not-the-actual-world'
        before=c.execute('SELECT COUNT(*) FROM character_life_activities').fetchone()[0]
        with pytest.raises(LifeActivityError,match='不属于'):
            LifeActivityService.start(c,person,current(e,c),'rest',12)
        assert c.execute('SELECT COUNT(*) FROM character_life_activities').fetchone()[0]==before


def test_repeat_processing_and_finish_do_not_double_credit_or_events(rest_world):
    e=rest_world;blocked(e)
    with e.db.write() as c:c.execute('UPDATE characters SET energy=10 WHERE id=?',(e.npc,))
    aid=start(e,e.npc)
    e.clock.heartbeat(e.wid,elapsed_seconds=12*60)
    with e.db.write() as c:
        at=current(e,c)
        heartbeat=c.execute('SELECT id FROM world_heartbeats WHERE world_id=? ORDER BY rowid DESC LIMIT 1',(e.wid,)).fetchone()[0]
        energy=c.execute('SELECT energy FROM characters WHERE id=?',(e.npc,)).fetchone()[0]
        row=c.execute('SELECT * FROM character_life_activities WHERE id=?',(aid,)).fetchone()
        count=c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
        LifeActivityService.advance(c,e.wid,at,heartbeat)
        LifeActivityService.finish(c,row,at,'completed','重复处理不能重复结算。')
        assert c.execute('SELECT energy FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==energy
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]==count
