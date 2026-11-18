"""载具移除不得绕过真实在车成员的安全下车流程。"""
import pytest

from test_rides import ride_world, boarded, choose, advance, arrival_seconds, current, get  # noqa: F401
from test_ride_recovery import blocked
from world_engine.removal import ElementRemovalSubmit, WorldElementRemover


def snapshot(c, e, rid):
    return {
        'vehicle': tuple(c.execute('SELECT * FROM vehicles WHERE id=?', (e.vehicle,)).fetchone()),
        'vehicle_state': tuple(c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()),
        'ride': tuple(c.execute('SELECT * FROM vehicle_rides WHERE id=?', (rid,)).fetchone()),
        'members': [tuple(r) for r in c.execute('SELECT * FROM vehicle_ride_members WHERE ride_id=? ORDER BY ordinal', (rid,))],
        'characters': [tuple(r) for r in c.execute('SELECT id, longitude, latitude, current_location_id, location_id, space_layer, active_vehicle_id, movement_type, movement_speed_kmh, health FROM characters WHERE id IN (?,?) ORDER BY id', (e.player, e.npc))],
        'movements': [tuple(r) for r in c.execute('SELECT * FROM character_movements WHERE vehicle_id=? ORDER BY id', (e.vehicle,))],
        'version': c.execute('SELECT version FROM worlds WHERE id=?', (e.wid,)).fetchone()[0],
    }


def remove(c, e, rid, key):
    ride = c.execute('SELECT proposal_event_id FROM vehicle_rides WHERE id=?', (rid,)).fetchone()
    return WorldElementRemover().submit(c, world_id=e.wid, request=ElementRemovalSubmit(
        requested_by_character_id=e.player, source_event_id=ride['proposal_event_id'],
        idempotency_key=key, target_element_type='vehicle', target_entity_id=e.vehicle,
        reason='destroyed', details='隔离验收：车辆事故移除请求。'))


@pytest.mark.parametrize('state', ['boarding', 'moving', 'blocked', 'arrived'])
def test_occupied_vehicle_rejects_removal_without_changing_physics(ride_world, state):
    e = ride_world
    if state == 'blocked':
        rid = blocked(e)
    else:
        rid = boarded(e)
        if state != 'boarding':
            choose(e, e.player, 'depart', rid)
        if state == 'arrived':
            advance(e, arrival_seconds(e, rid))
    with e.db.write() as c:
        before = snapshot(c, e, rid)
        result = remove(c, e, rid, 'removal-occupied-' + state)
        assert result.status == 'rejected'
        assert '在车' in result.rejection_reason
        assert not result.effects
        assert snapshot(c, e, rid) == before
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE event_type='world.element_removed' AND world_id=?", (e.wid,)).fetchone()[0] == 0


def test_removal_after_everyone_disembarks_is_allowed(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.npc, 'disembark', rid)
    choose(e, e.player, 'disembark', rid)
    with e.db.write() as c:
        result = remove(c, e, rid, 'removal-empty-ride')
        assert result.status == 'applied'
        assert c.execute('SELECT is_available FROM vehicles WHERE id=?', (e.vehicle,)).fetchone()[0] == 0


def test_one_remaining_member_blocks_even_if_not_live_or_alive(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'disembark', rid)
    with e.db.write() as c:
        # 哥哥，生命值与关系标志不代表实际下车，仍须保留物理位置。
        c.execute('UPDATE characters SET health=0 WHERE id=?', (e.npc,))
        c.execute('UPDATE vehicle_ride_members SET live=0 WHERE ride_id=? AND character_id=?', (rid, e.npc))
        before = snapshot(c, e, rid)
        result = remove(c, e, rid, 'removal-one-occupant')
        assert result.status == 'rejected'
        assert snapshot(c, e, rid) == before
