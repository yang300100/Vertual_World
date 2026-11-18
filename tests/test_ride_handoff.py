"""哥哥，隔离存档验证有限驾驶交接、原路线与恢复兼容，绝不代替真实 NPC 选择。"""
# ruff: noqa: F811
import json
import sqlite3
from datetime import timedelta

import pytest
from pydantic import ValidationError

from test_rides import ride_world  # noqa: F401
from test_rides import advance, arrival_seconds, choose, current, get, members
from test_ride_recovery import blocked, physical, propose as propose_recovery, accept_recovery
from test_ride_runtime import rental
from world_engine.contracts import ContractService
from world_engine.database import Database
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso
from world_engine.ride_handoff import RideHandoffService
from world_engine.rides import RideIntent, RideService


def proposal(e, rid, *, proposer=None, driver=None, seconds=3600):
    with e.db.read() as c:
        deadline = current(e, c) + timedelta(seconds=seconds)
    return choose(e, proposer or e.player, 'propose_handoff', rid,
                  controller_id=driver or e.npc, deadline_world_time=deadline)['handoff_id']


def plan(e, hid):
    with e.db.read() as c:
        return dict(c.execute('SELECT * FROM vehicle_ride_handoffs WHERE id=?', (hid,)).fetchone())


def consent(e, rid, hid, *, party=None, proposer=None):
    for cid in party or [e.player, e.npc]:
        if cid != (proposer or e.player):
            choose(e, cid, 'accept_handoff', rid, handoff_id=hid)


def ready(e, rid, *, driver=None, seconds=3600, party=None):
    hid = proposal(e, rid, driver=driver, seconds=seconds)
    choose(e, e.player, 'authorize_handoff', rid, handoff_id=hid)
    consent(e, rid, hid, party=party)
    return hid


def take(e, rid, hid, cid=None):
    return choose(e, cid or e.npc, 'take_control', rid, handoff_id=hid)


def suspend(e):
    with e.db.write() as c:
        RideService.suspend_vehicle(c, e.vehicle, current(e, c), '隔离规则：本次真实行程原位挂起')


def test_tired_original_driver_can_grant_but_only_new_driver_takes_unchanged_real_route(ride_world, monkeypatch):
    e = ride_world; rid = blocked(e, party=[e.player, e.npc, e.other]); before = physical(e, rid)
    original = get(e, rid); old_members = members(e, rid)
    with e.db.write() as c:
        c.execute('UPDATE characters SET energy=10 WHERE id=?', (e.player,))
    hid = ready(e, rid, party=[e.player, e.npc, e.other])
    monkeypatch.setattr(MovementService, '_plan_route', lambda *a, **k: (_ for _ in ()).throw(AssertionError('交接不能重规划原路线')))
    result = take(e, rid, hid)
    assert result['status'] == 'moving' and result['effective_controller_id'] == e.npc
    assert get(e, rid)['controller_id'] == original['controller_id'] == e.player
    assert get(e, rid)['terms_json'] == original['terms_json'] and members(e, rid) == old_members
    after = physical(e, rid)
    for key in ['id', 'character_id', 'route_json', 'started_at_world', 'distance_travelled_km']:
        assert after['move'][key] == before['move'][key]
    monkeypatch.undo()
    e.clock.heartbeat(e.wid, elapsed_seconds=arrival_seconds(e, rid) + 1)
    assert get(e, rid)['status'] == 'arrived'
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 1
        arrived = c.execute("SELECT actor_id FROM world_events WHERE event_type='social.ride_arrived' AND json_extract(payload_json,'$.ride_id')=?", (rid,)).fetchone()[0]
        assert arrived == e.npc
        for cid in [e.player, e.npc, e.other]:
            assert RideService.aboard(c, cid)['id'] == rid
    for cid in [e.other, e.npc, e.player]:
        choose(e, cid, 'disembark', rid)
    assert get(e, rid)['status'] == 'ended'


def test_permission_all_personal_choices_and_takeover_are_separate_receipts(ride_world):
    e = ride_world; rid = blocked(e, party=[e.player, e.npc, e.other]); before = physical(e, rid)
    hid = proposal(e, rid)
    grant = choose(e, e.player, 'authorize_handoff', rid, handoff_id=hid)['event_id']
    choose(e, e.npc, 'accept_handoff', rid, handoff_id=hid)
    with pytest.raises(ValueError): take(e, rid, hid)
    assert plan(e, hid)['status'] == 'pending' and physical(e, rid) == before
    choose(e, e.other, 'accept_handoff', rid, handoff_id=hid)
    assert plan(e, hid)['status'] == 'accepted' and physical(e, rid) == before
    taken = take(e, rid, hid)['event_id']
    assert taken != grant
    assert take(e, rid, hid)['event_id'] == taken
    assert choose(e, e.player, 'authorize_handoff', rid, handoff_id=hid)['event_id'] == grant


def test_passenger_consents_do_not_grant_vehicle_right(ride_world):
    e = ride_world; rid = blocked(e); hid = proposal(e, rid); consent(e, rid, hid); before = physical(e, rid)
    with pytest.raises(ValueError, match='授权'): take(e, rid, hid)
    assert physical(e, rid) == before
    for cid in [e.npc, e.outside]:
        with pytest.raises(ValueError): choose(e, cid, 'authorize_handoff', rid, handoff_id=hid)
    choose(e, e.player, 'authorize_handoff', rid, handoff_id=hid)
    for cid in [e.player, e.outside]:
        with pytest.raises(ValueError): take(e, rid, hid, cid)
    assert physical(e, rid) == before


@pytest.mark.parametrize('op', ['decline_handoff', 'withdraw_handoff'])
def test_personal_decline_or_withdraw_preserves_every_earlier_choice(ride_world, op):
    e = ride_world; rid = blocked(e, party=[e.player, e.npc, e.other]); hid = proposal(e, rid)
    choose(e, e.npc, 'accept_handoff', rid, handoff_id=hid)
    if op == 'withdraw_handoff': choose(e, e.other, 'accept_handoff', rid, handoff_id=hid)
    with e.db.read() as c:
        original = list(c.execute('SELECT * FROM vehicle_ride_handoff_members WHERE handoff_id=? ORDER BY ordinal', (hid,)))
    before = physical(e, rid)
    choose(e, e.other if op == 'decline_handoff' else e.npc, op, rid, handoff_id=hid)
    assert plan(e, hid)['status'] in {'declined', 'withdrawn'}
    with pytest.raises(ValueError): take(e, rid, hid)
    with e.db.read() as c:
        rows = list(c.execute('SELECT * FROM vehicle_ride_handoff_members WHERE handoff_id=? ORDER BY ordinal', (hid,)))
        assert [tuple(r) for r in rows[:2]] == [tuple(r) for r in original[:2]]
    assert physical(e, rid) == before


def test_holder_may_revoke_before_execution_without_erasing_grant(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid); original = plan(e, hid); before = physical(e, rid)
    revoked = choose(e, e.player, 'revoke_handoff', rid, handoff_id=hid)['event_id']
    assert plan(e, hid)['status'] == 'revoked' and plan(e, hid)['authorization_event_id'] == original['authorization_event_id']
    assert choose(e, e.player, 'revoke_handoff', rid, handoff_id=hid)['event_id'] == revoked
    with pytest.raises(ValueError): take(e, rid, hid)
    assert physical(e, rid) == before


def test_revoke_during_motion_keeps_actual_driver_and_all_people_at_actual_position(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid); take(e, rid, hid); advance(e, 5)
    before = physical(e, rid)
    choose(e, e.player, 'revoke_handoff', rid, handoff_id=hid)
    assert get(e, rid)['status'] == 'blocked'
    advance(e, 20)
    after = physical(e, rid)
    assert before['people'] == after['people'] and before['move']['distance_travelled_km'] == after['move']['distance_travelled_km']
    with e.db.read() as c:
        assert RideService.effective_controller(c, get(e, rid), current(e, c)) == e.npc
    with pytest.raises(ValueError): propose_recovery(e, rid)


@pytest.mark.parametrize('change', ['capacity', 'route', 'progress', 'position', 'layer', 'stop', 'block'])
def test_changed_actual_snapshot_cannot_reuse_all_previous_consents(ride_world, change):
    e = ride_world; rid = blocked(e); hid = ready(e, rid)
    with e.db.write() as c:
        if change == 'capacity': c.execute('UPDATE vehicle_states SET capacity=2 WHERE vehicle_id=?', (e.vehicle,))
        elif change == 'route': c.execute("UPDATE character_movements SET route_json='{}' WHERE id=?", (get(e, rid)['movement_id'],))
        elif change == 'progress': c.execute('UPDATE character_movements SET distance_travelled_km=distance_travelled_km+0.01 WHERE id=?', (get(e, rid)['movement_id'],))
        elif change == 'position': c.execute('UPDATE characters SET longitude=longitude+0.01 WHERE id=?', (e.npc,))
        elif change == 'layer': c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (e.npc,))
    if change == 'stop': choose(e, e.npc, 'request_stop', rid)
    if change == 'block': suspend(e)
    before = physical(e, rid)
    with pytest.raises(ValueError): take(e, rid, hid)
    assert physical(e, rid) == before


@pytest.mark.parametrize('change', ['holder', 'vehicle', 'registration', 'future_registration'])
def test_changed_real_authority_never_reuses_the_grant(ride_world, change):
    e = ride_world; rid = blocked(e); hid = ready(e, rid)
    with e.db.write() as c:
        if change == 'holder': c.execute('UPDATE vehicles SET owner_character_id=? WHERE id=?', (e.other, e.vehicle))
        elif change == 'vehicle': c.execute('UPDATE vehicles SET is_available=0 WHERE id=?', (e.vehicle,))
        elif change == 'registration': c.execute("UPDATE element_registration_requests SET status='rejected' WHERE result_entity_id=?", (e.vehicle,))
        else:
            future = to_iso(current(e, c) + timedelta(minutes=1))
            c.execute('UPDATE world_events SET occurred_at=? WHERE id=(SELECT source_event_id FROM element_registration_requests WHERE result_entity_id=?)', (future, e.vehicle))
    before = physical(e, rid)
    with pytest.raises(ValueError): take(e, rid, hid)
    assert physical(e, rid) == before


def test_lease_holder_not_owner_is_authority_and_grant_never_extends_paid_lease(ride_world):
    e = ride_world; rental(e, seconds=3600); rid = blocked(e)
    with pytest.raises(ValueError, match='租约'): proposal(e, rid, seconds=7200)
    hid = proposal(e, rid, seconds=1000)
    assert json.loads(plan(e, hid)['terms_json'])['authority_source']['holder_id'] == e.player
    with pytest.raises(ValueError): choose(e, e.npc, 'authorize_handoff', rid, handoff_id=hid)
    choose(e, e.player, 'authorize_handoff', rid, handoff_id=hid); consent(e, rid, hid); take(e, rid, hid)
    with e.db.read() as c:
        assert ContractService.vehicle_for(c, e.wid, e.npc, e.vehicle, at=current(e, c)) is None
    with e.db.write() as c:
        contract = c.execute("SELECT * FROM contract_fulfillments WHERE asset_id=? AND status='active'", (e.vehicle,)).fetchone()
        ContractService.cancel(c, contract, to_iso(current(e, c)))
    before = physical(e, rid); advance(e, 20)
    assert get(e, rid)['status'] == 'blocked' and physical(e, rid)['move']['distance_travelled_km'] == before['move']['distance_travelled_km']


@pytest.mark.parametrize('change', ['health', 'energy', 'satiety', 'fuel'])
def test_new_driver_and_vehicle_must_remain_able_to_drive(ride_world, change):
    e = ride_world; rid = blocked(e); hid = ready(e, rid)
    with e.db.write() as c:
        if change == 'fuel': c.execute('UPDATE vehicles SET metadata_json=? WHERE id=?', (json.dumps({'fuel': 5}), e.vehicle))
        else: c.execute('UPDATE characters SET ' + change + '=0 WHERE id=?', (e.npc,))
    before = physical(e, rid)
    with pytest.raises(ValueError): take(e, rid, hid)
    assert physical(e, rid) == before


def test_grant_is_specific_to_ride_and_never_changes_vehicle_ownership_or_original_acceptance(ride_world):
    e = ride_world; rid = blocked(e); old = get(e, rid); own = members(e, rid); hid = ready(e, rid); take(e, rid, hid)
    with e.db.read() as c:
        assert ContractService.vehicle_for(c, e.wid, e.npc, e.vehicle, at=current(e, c)) is None
        assert c.execute('SELECT owner_character_id FROM vehicles WHERE id=?', (e.vehicle,)).fetchone()[0] == e.player
    assert get(e, rid)['terms_json'] == old['terms_json'] and members(e, rid) == own


def test_finite_grant_can_continue_after_old_deadline_and_does_not_expire_by_old_terms(ride_world):
    e = ride_world; rid = blocked(e); advance(e, 3 * 3600); hid = ready(e, rid); take(e, rid, hid)
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


def test_expired_pending_grant_cannot_be_taken_or_reused(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid, seconds=400); advance(e, 401); before = physical(e, rid)
    assert plan(e, hid)['status'] == 'expired'
    with pytest.raises(ValueError): take(e, rid, hid)
    assert physical(e, rid) == before


def test_current_driver_can_resume_later_pause_within_original_finite_permission(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid, seconds=7200); take(e, rid, hid); advance(e, 5); suspend(e)
    recid = propose_recovery(e, rid, seconds=1000); accept_recovery(e, rid, recid)
    with pytest.raises(ValueError): choose(e, e.player, 'resume', rid, recovery_id=recid)
    choose(e, e.npc, 'resume', rid, recovery_id=recid)
    with e.db.read() as c:
        row = c.execute('SELECT * FROM vehicle_ride_recoveries WHERE id=?', (recid,)).fetchone()
        assert row['controller_id'] == e.npc and json.loads(row['snapshot_json'])['control_source']['handoff_id'] == hid
        assert RideService.effective_deadline(c, get(e, rid), current(e, c)) == from_iso(row['deadline_at'])
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


def test_recovery_after_takeover_cannot_extend_finite_permission(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid, seconds=1000); take(e, rid, hid); advance(e, 5); suspend(e)
    with pytest.raises(ValueError, match='有限驾驶期限'): propose_recovery(e, rid, seconds=3600)


def test_takeover_after_original_recovery_uses_new_execution_and_preserves_old_sources(ride_world):
    e = ride_world; rid = blocked(e); recid = propose_recovery(e, rid, seconds=400); accept_recovery(e, rid, recid)
    choose(e, e.player, 'resume', rid, recovery_id=recid); advance(e, 5); suspend(e)
    hid = ready(e, rid, seconds=3600); take(e, rid, hid)
    advance(e, 401)
    assert get(e, rid)['status'] == 'arrived'
    with e.db.read() as c:
        assert c.execute('SELECT status FROM vehicle_ride_recoveries WHERE id=?', (recid,)).fetchone()[0] == 'executed'
        assert RideHandoffService.governing(c, get(e, rid), current(e, c))[0] == 'handoff'


def test_two_handoffs_choose_latest_actual_execution_including_same_world_time(ride_world):
    e = ride_world; rid = blocked(e, party=[e.player, e.npc, e.other]); first = ready(e, rid, seconds=7200, party=[e.player, e.npc, e.other]); take(e, rid, first); suspend(e)
    second = ready(e, rid, driver=e.other, seconds=3600, party=[e.player, e.npc, e.other]); take(e, rid, second, e.other)
    with e.db.read() as c:
        assert RideService.effective_controller(c, get(e, rid), current(e, c)) == e.other
        assert RideService.effective_deadline(c, get(e, rid), current(e, c)) == from_iso(plan(e, second)['deadline_at'])
    assert plan(e, first)['status'] == 'executed'
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


@pytest.mark.parametrize('first', ['handoff', 'recovery'])
def test_pending_handoff_and_recovery_cannot_silently_replace_each_other(ride_world, first):
    e = ride_world; rid = blocked(e)
    if first == 'handoff':
        proposal(e, rid)
        with pytest.raises(ValueError): propose_recovery(e, rid)
    else:
        propose_recovery(e, rid)
        with pytest.raises(ValueError): proposal(e, rid)


def test_takeover_savepoint_rolls_back_execution_event_and_all_physics_when_late_write_fails(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid)
    with e.db.write() as c:
        c.execute("CREATE TRIGGER fail_handoff BEFORE UPDATE OF status ON vehicle_states WHEN NEW.status='moving' BEGIN SELECT RAISE(ABORT,'隔离交接回滚哨兵'); END")
        count = c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
    before = physical(e, rid); original = plan(e, hid)
    with pytest.raises(sqlite3.IntegrityError, match='回滚哨兵'): take(e, rid, hid)
    assert physical(e, rid) == before and plan(e, hid) == original
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0] == count


def test_restart_is_idempotent_and_retains_real_driver_route_and_own_receipt(ride_world):
    e = ride_world; rid = blocked(e); hid = ready(e, rid); event = take(e, rid, hid)['event_id']; advance(e, 5); before = physical(e, rid)
    e.db = e.database = Database(e.settings.database_path); e.db.initialize(); e.db.initialize()
    assert physical(e, rid) == before and take(e, rid, hid)['event_id'] == event
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


def test_existing_database_migration_creates_no_grants_or_rewrites_old_recovery_snapshot(ride_world):
    e = ride_world; rid = blocked(e); recid = propose_recovery(e, rid); accept_recovery(e, rid, recid)
    with e.db.write() as c:
        old = dict(c.execute('SELECT * FROM vehicle_ride_recoveries WHERE id=?', (recid,)).fetchone())
        c.execute('DROP TABLE vehicle_ride_handoff_members'); c.execute('DROP TABLE vehicle_ride_handoffs')
    before = physical(e, rid)
    e.db = e.database = Database(e.settings.database_path); e.db.initialize(); e.db.initialize()
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM vehicle_ride_handoffs').fetchone()[0] == 0
        assert dict(c.execute('SELECT * FROM vehicle_ride_recoveries WHERE id=?', (recid,)).fetchone()) == old
    assert physical(e, rid) == before
    choose(e, e.player, 'resume', rid, recovery_id=recid)
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


def test_ended_lease_requires_current_real_owner_to_authorize_a_whole_new_round(ride_world):
    e = ride_world; rental(e, seconds=3600); rid = blocked(e); first = ready(e, rid, seconds=1000); take(e, rid, first); advance(e, 5)
    with e.db.write() as c:
        contract = c.execute("SELECT * FROM contract_fulfillments WHERE asset_id=? AND status='active'", (e.vehicle,)).fetchone()
        ContractService.cancel(c, contract, to_iso(current(e, c)))
    old = plan(e, first); second = proposal(e, rid, driver=e.npc)
    assert plan(e, second)['holder_id'] == e.npc
    with pytest.raises(ValueError): choose(e, e.player, 'authorize_handoff', rid, handoff_id=second)
    with pytest.raises(ValueError): take(e, rid, second)
    choose(e, e.npc, 'authorize_handoff', rid, handoff_id=second)
    with pytest.raises(ValueError): take(e, rid, second)
    consent(e, rid, second); take(e, rid, second)
    assert plan(e, first) == old
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


def test_original_driver_must_use_recovery_instead_of_self_handoff(ride_world):
    e = ride_world; rid = blocked(e)
    with pytest.raises(ValueError, match='继续驾驶'): proposal(e, rid, driver=e.player)


@pytest.mark.parametrize('expired', [False, True])
def test_same_actual_driver_needs_a_whole_new_grant_and_all_consents_after_revocation_or_expiry(ride_world, expired):
    e = ride_world; rid = blocked(e); first = ready(e, rid, seconds=1000); take(e, rid, first); advance(e, 5)
    if expired:
        suspend(e); advance(e, 1001)
    else:
        choose(e, e.player, 'revoke_handoff', rid, handoff_id=first)
    original = plan(e, first); before = physical(e, rid)
    second = proposal(e, rid, driver=e.npc)
    with pytest.raises(ValueError): take(e, rid, second)
    choose(e, e.player, 'authorize_handoff', rid, handoff_id=second)
    with pytest.raises(ValueError): take(e, rid, second)
    assert physical(e, rid) == before
    consent(e, rid, second); take(e, rid, second)
    assert plan(e, first) == original
    assert plan(e, first)['authorization_event_id'] != plan(e, second)['authorization_event_id']
    with e.db.read() as c:
        assert RideService.effective_controller(c, get(e, rid), current(e, c)) == e.npc
    advance(e, arrival_seconds(e, rid)); assert get(e, rid)['status'] == 'arrived'


@pytest.mark.parametrize('change', ['grant_source', 'consent_source', 'execution_source', 'route', 'driver', 'vehicle'])
def test_invalid_running_authority_blocks_before_any_further_distance(ride_world, change):
    e = ride_world; rid = blocked(e); hid = ready(e, rid); take(e, rid, hid); before = physical(e, rid)
    with e.db.write() as c:
        if change == 'grant_source': eid = plan(e, hid)['authorization_event_id']
        elif change == 'execution_source': eid = plan(e, hid)['execution_event_id']
        elif change == 'consent_source': eid = c.execute('SELECT decision_event_id FROM vehicle_ride_handoff_members WHERE handoff_id=? AND character_id=?', (hid, e.npc)).fetchone()[0]
        elif change == 'route': c.execute("UPDATE character_movements SET route_json='{}' WHERE id=?", (get(e, rid)['movement_id'],))
        elif change == 'driver': c.execute('UPDATE characters SET energy=0 WHERE id=?', (e.npc,))
        else: c.execute('UPDATE vehicles SET is_available=0 WHERE id=?', (e.vehicle,))
        if change.endswith('_source'): c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (eid,))
    advance(e, 20)
    after = physical(e, rid)
    assert get(e, rid)['status'] == 'blocked'
    assert after['move']['distance_travelled_km'] == before['move']['distance_travelled_km']
    assert [(p[0], p[1], p[2]) for p in after['people']] == [(p[0], p[1], p[2]) for p in before['people']]


def test_original_stop_request_survives_takeover_and_final_safe_arrival(ride_world):
    e = ride_world; rid = blocked(e, stop=True); hid = ready(e, rid); take(e, rid, hid)
    assert get(e, rid)['stop_requested'] == 1
    assert json.loads(plan(e, hid)['terms_json'])['stop_requested'] is True
    with pytest.raises(ValueError): choose(e, e.npc, 'disembark', rid)
    advance(e, arrival_seconds(e, rid))
    assert get(e, rid)['status'] == 'arrived' and get(e, rid)['stop_requested'] == 1


@pytest.mark.parametrize('extra', [{'vehicle_id': 'x'}, {'member_ids': ['a']}, {'recovery_id': 'r'}, {'deadline_world_time': '2030-01-01T10:00:00Z'}])
def test_handoff_answers_cannot_edit_frozen_vehicle_members_or_deadline(extra):
    with pytest.raises(ValidationError):
        RideIntent.model_validate({'operation': 'accept_handoff', 'ride_id': 'ride', 'handoff_id': 'handoff', 'statement': '本人明确选择本轮。', **extra})
