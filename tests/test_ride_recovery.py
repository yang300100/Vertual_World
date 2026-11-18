"""哥哥，恢复测试只用隔离世界，暂停不会凭空变成到达。"""
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from test_rides import ride_world  # noqa: F401
from test_rides import boarded, choose, advance, arrival_seconds, current, get, members
from world_engine.database import Database
from world_engine.repository import to_iso
from world_engine.rides import RideIntent, RideService
from world_engine.ride_recovery import RideRecoveryService


def blocked(e, *, stop=False, party=None):
    rid = boarded(e, **({'member_ids': party} if party else {}))
    choose(e, e.player, 'depart', rid)
    if stop:
        choose(e, e.npc, 'request_stop', rid)
    advance(e, arrival_seconds(e, rid) / 2)
    with e.db.write() as c:
        RideService.suspend_vehicle(c, e.vehicle, current(e, c), '隔离规则事件：途中暂缓原行程')
    return rid


def propose(e, rid, *, cid=None, seconds=3600):
    with e.db.read() as c:
        deadline = current(e, c) + timedelta(seconds=seconds)
    return choose(e, cid or e.player, 'propose_recovery', rid, deadline_world_time=deadline)['recovery_id']


def recovery(e, recid):
    with e.db.read() as c:
        return dict(c.execute('SELECT * FROM vehicle_ride_recoveries WHERE id=?', (recid,)).fetchone())


def accept_recovery(e, rid, recid, party=None):
    for cid in party or [e.npc]:
        choose(e, cid, 'accept_recovery', rid, recovery_id=recid)


def physical(e, rid):
    with e.db.read() as c:
        return {
            'people': [tuple(r) for r in c.execute('SELECT id,longitude,latitude,money,energy,satiety FROM characters WHERE world_id=? ORDER BY id', (e.wid,))],
            'vehicle': tuple(c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()),
            'move': dict(c.execute('SELECT * FROM character_movements WHERE id=?', (get(e, rid)['movement_id'],)).fetchone()),
            'old_terms': get(e, rid)['terms_json'], 'old_deadline': get(e, rid)['deadline_at'],
            'members': members(e, rid),
        }


def test_proposal_and_each_acceptance_preserve_blocked_position_and_old_sources(ride_world):
    e = ride_world;rid = blocked(e);before = physical(e, rid);recid = propose(e, rid)
    assert recovery(e, recid)['status'] == 'pending'
    assert physical(e, rid) == before
    with e.db.read() as c:
        actor = c.execute('SELECT * FROM characters WHERE id=?', (e.player,)).fetchone()
        view = RideService.rows(c, actor, current(e, c))[0]
        assert view['status'] == 'blocked' and view['recovery']['status'] == 'pending'
        assert [m['decision'] for m in view['recovery']['members']] == ['accepted', 'pending']
    with pytest.raises(ValueError, match='全员'):
        choose(e, e.player, 'resume', rid, recovery_id=recid)
    accept_recovery(e, rid, recid)
    assert physical(e, rid) == before
    assert recovery(e, recid)['status'] == 'accepted'


def test_real_remaining_route_resumes_same_movement_after_old_eta_without_teleport(ride_world):
    e = ride_world;rid = blocked(e);before = physical(e, rid)
    advance(e, 3 * 3600)
    recid = propose(e, rid);accept_recovery(e, rid, recid)
    result = choose(e, e.player, 'resume', rid, recovery_id=recid)
    assert result['status'] == 'moving' and result['recovery']['status'] == 'executed'
    after = physical(e, rid)
    assert after['people'] == before['people']
    assert after['move']['id'] == before['move']['id']
    assert after['move']['route_json'] == before['move']['route_json']
    assert after['move']['distance_travelled_km'] == before['move']['distance_travelled_km']
    assert after['old_terms'] == before['old_terms'] and after['old_deadline'] == before['old_deadline']
    assert arrival_seconds(e, rid) > 0
    advance(e, arrival_seconds(e, rid) / 2)
    assert get(e, rid)['status'] == 'moving'
    advance(e, arrival_seconds(e, rid))
    assert get(e, rid)['status'] == 'arrived'
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 1
        for cid in [e.player, e.npc]:
            assert RideService.aboard(c, cid)


def test_decline_is_retained_and_cannot_be_overwritten_or_auto_resumed(ride_world):
    e = ride_world;rid = blocked(e);recid = propose(e, rid);before = physical(e, rid)
    rejected = choose(e, e.npc, 'decline_recovery', rid, recovery_id=recid)
    assert recovery(e, recid)['status'] == 'declined'
    with pytest.raises(ValueError):
        choose(e, e.npc, 'accept_recovery', rid, recovery_id=recid)
    with pytest.raises(ValueError):
        choose(e, e.player, 'resume', rid, recovery_id=recid)
    assert choose(e, e.npc, 'decline_recovery', rid, recovery_id=recid)['event_id'] == rejected['event_id']
    assert physical(e, rid) == before


def test_accepted_member_can_withdraw_before_driver_resumes(ride_world):
    e = ride_world;rid = blocked(e);recid = propose(e, rid);accept_recovery(e, rid, recid)
    original = recovery(e, recid)
    with e.db.read() as c:
        accepted = c.execute('SELECT decision_event_id FROM vehicle_ride_recovery_members WHERE recovery_id=? AND character_id=?', (recid, e.npc)).fetchone()[0]
    choose(e, e.npc, 'withdraw_recovery', rid, recovery_id=recid)
    assert recovery(e, recid)['status'] == 'withdrawn'
    with e.db.read() as c:
        assert c.execute('SELECT decision_event_id FROM vehicle_ride_recovery_members WHERE recovery_id=? AND character_id=?', (recid, e.npc)).fetchone()[0] == accepted
    with pytest.raises(ValueError):
        choose(e, e.player, 'resume', rid, recovery_id=recid)
    assert get(e, rid)['status'] == 'blocked' and original['status'] == 'accepted'


def test_original_stop_policy_and_stop_receipt_are_preserved(ride_world):
    e = ride_world;rid = blocked(e, stop=True);recid = propose(e, rid)
    accept_recovery(e, rid, recid);choose(e, e.player, 'resume', rid, recovery_id=recid)
    assert get(e, rid)['stop_requested'] == 1
    assert json.loads(recovery(e, recid)['terms_json'])['stop_requested'] is True
    with pytest.raises(ValueError, match='行驶'):
        choose(e, e.npc, 'disembark', rid)


def test_new_stop_request_invalidates_pending_recovery_but_does_not_clear_previous_decisions(ride_world):
    e = ride_world;rid = blocked(e);recid = propose(e, rid);accept_recovery(e, rid, recid)
    choose(e, e.npc, 'request_stop', rid)
    assert recovery(e, recid)['status'] == 'invalidated'
    with pytest.raises(ValueError):
        choose(e, e.player, 'resume', rid, recovery_id=recid)
    assert get(e, rid)['stop_requested'] == 1


@pytest.mark.parametrize('changed', ['capacity', 'route', 'progress', 'position', 'layer', 'source'])
def test_changed_physics_or_source_cannot_reuse_previous_acceptance(ride_world, changed):
    e = ride_world;rid = blocked(e);recid = propose(e, rid);accept_recovery(e, rid, recid)
    with e.db.write() as c:
        if changed == 'capacity':c.execute('UPDATE vehicle_states SET capacity=2 WHERE vehicle_id=?', (e.vehicle,))
        elif changed == 'route':c.execute("UPDATE character_movements SET route_json='{}' WHERE id=?", (get(e, rid)['movement_id'],))
        elif changed == 'progress':c.execute('UPDATE character_movements SET distance_travelled_km=distance_travelled_km+0.01 WHERE id=?', (get(e, rid)['movement_id'],))
        elif changed == 'position':c.execute('UPDATE characters SET longitude=longitude+0.01 WHERE id=?', (e.npc,))
        elif changed == 'layer':c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (e.npc,))
        else:c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (recovery(e, recid)['proposal_event_id'],))
    before = physical(e, rid)
    with pytest.raises(ValueError):choose(e, e.player, 'resume', rid, recovery_id=recid)
    assert physical(e, rid) == before


def test_only_original_controller_may_resume_and_outsider_cannot_answer(ride_world):
    e = ride_world;rid = blocked(e);recid = propose(e, rid);accept_recovery(e, rid, recid)
    for cid in [e.npc,e.outside]:
        with pytest.raises(ValueError):choose(e, cid, 'resume', rid, recovery_id=recid)
    with pytest.raises(ValueError):choose(e, e.outside, 'accept_recovery', rid, recovery_id=recid)


def test_epoch_and_restart_preserve_single_route_and_own_idempotent_events(ride_world):
    e = ride_world;rid = blocked(e);first = propose(e, rid);accepted_event = choose(e, e.npc, 'accept_recovery', rid, recovery_id=first)['event_id']
    assert choose(e, e.npc, 'accept_recovery', rid, recovery_id=first)['event_id'] == accepted_event
    resumed_event = choose(e, e.player, 'resume', rid, recovery_id=first)['event_id']
    assert choose(e, e.player, 'resume', rid, recovery_id=first)['event_id'] == resumed_event
    advance(e, arrival_seconds(e, rid) / 2)
    with e.db.write() as c:RideService.suspend_vehicle(c, e.vehicle, current(e,c), '隔离规则：第二次途中挂起')
    second = propose(e, rid)
    assert recovery(e, second)['epoch'] == recovery(e, first)['epoch'] + 1
    with pytest.raises(ValueError):choose(e,e.player,'resume',rid,recovery_id=second)
    before = physical(e,rid)
    e.db = e.database = Database(e.settings.database_path);e.db.initialize()
    assert physical(e,rid)==before
    accept_recovery(e,rid,second);choose(e,e.player,'resume',rid,recovery_id=second)
    advance(e,arrival_seconds(e,rid));assert get(e,rid)['status']=='arrived'


def test_invalid_operation_shapes_cannot_change_frozen_route_or_deadline():
    base={'operation':'accept_recovery','ride_id':'r','recovery_id':'n','statement':'本人接受此轮'}
    for extra in ({'vehicle_id':'v'},{'member_ids':['a']},{'deadline_world_time':'2030-01-01T10:00:00Z'},{'controller_id':'x'}):
        with pytest.raises(ValidationError):RideIntent(**(base|extra))
    with pytest.raises(ValidationError):RideIntent(operation='propose_recovery',ride_id='r',deadline_world_time='2030-01-01T10:00:00',statement='需要时区')
    with pytest.raises(ValidationError):RideIntent(operation='accept',ride_id='r',recovery_id='n',statement='不能夹带轮次')


def test_remaining_time_uses_each_edge_speed_and_original_serialization_rounding():
    movement={'distance_travelled_km':1.5,'route_index':1,'total_distance_km':3,'speed_kmh':200,'route_json':json.dumps({'polyline':[[0,0],[1,0],[2,0]],'edges':[{'length_km':1,'speed_kmh':2},{'length_km':2,'speed_kmh':4}]})}
    distance,seconds=RideRecoveryService.remaining(movement)
    assert distance==1.5 and seconds==1350


def test_effective_deadline_reserves_only_full_new_agreement_and_reverts_after_withdrawal(ride_world):
    e=ride_world;rid=blocked(e);recid=propose(e,rid,seconds=6*3600)
    with e.db.read() as c:
        row=c.execute('SELECT * FROM vehicle_rides WHERE id=?',(rid,)).fetchone()
        old=RideService.effective_deadline(c,row,current(e,c))
        assert to_iso(old)==row['deadline_at']
    accept_recovery(e,rid,recid)
    with e.db.read() as c:
        row=c.execute('SELECT * FROM vehicle_rides WHERE id=?',(rid,)).fetchone()
        assert to_iso(RideService.effective_deadline(c,row,current(e,c)))==recovery(e,recid)['deadline_at']
    choose(e,e.npc,'withdraw_recovery',rid,recovery_id=recid)
    with e.db.read() as c:
        row=c.execute('SELECT * FROM vehicle_rides WHERE id=?',(rid,)).fetchone()
        assert RideService.effective_deadline(c,row,current(e,c))==old
