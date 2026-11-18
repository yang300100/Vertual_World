"""哥哥，所有共乘验收都使用独立小世界，绝不代操作正式人物。"""

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.actions import ActionService
from world_engine.clock import WorldClockService
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.movement import MovementService
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now
from world_engine.rides import RideIntent, RideService


@pytest.fixture
def ride_world(tmp_path):
    settings = Settings(database_path=tmp_path / 'rides.db', minutes_per_tick=60)
    db = Database(settings.database_path)
    db.initialize()
    repo = WorldRepository()
    at = datetime(2030, 1, 1, 9, tzinfo=UTC)
    with db.write() as c:
        wid = repo.create_world(c, name='独立真实共乘验收', minutes_per_tick=60, start_time=at, seed_maps=False)
        origin = dict(c.execute("SELECT * FROM locations WHERE world_id=? AND name='晨星广场'", (wid,)).fetchone())
        destinations = c.execute('SELECT id FROM locations WHERE world_id=? AND id<>? ORDER BY name', (wid, origin['id'])).fetchall()
        for index, destination in enumerate(destinations, 1):
            c.execute('UPDATE locations SET longitude=?,latitude=?,area_radius_km=0.03 WHERE id=?', (origin['longitude'] + 0.01 * index, origin['latitude'], destination[0]))
        c.execute('UPDATE locations SET area_radius_km=0.03 WHERE id=?', (origin['id'],))
        player = repo.create_player_character(c, world_id=wid, name='验收乘员', identity='旅人', location_id=origin['id'], traits=[], goal='真实共乘')
        npcs = [r[0] for r in c.execute('SELECT id FROM characters WHERE world_id=? AND is_player=0 ORDER BY name', (wid,))]
        ids = [player.id, *npcs]
        for index, cid in enumerate(ids):
            # 哥哥，这些是隔离场景的明确初始位置和过去经历，不是执行时补充状态。
            c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=?,current_room_id=NULL,current_fixture_id=NULL,space_layer='ground',active_vehicle_id=NULL,movement_type='land',movement_speed_kmh=5,health=100,energy=95,satiety=95,identity='旅人',traits_json='[]' WHERE id=?", (origin['longitude'] + index * 0.00001, origin['latitude'], origin['id'], origin['id'], cid))
            for lid in [origin['id'], *(d[0] for d in destinations)]:
                ActionService._record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at - timedelta(days=1), event_type='action.socialize', actor_id=cid, target_id=None, location_id=lid, summary='隔离初始历史：本人昨日实际到访此处。', payload={'fixture_initial_history': True})
        c.execute('UPDATE worlds SET current_time=? WHERE id=?', (to_iso(at), wid))
        c.execute('UPDATE world_clock SET time_scale=1,next_adjudication_world_time=? WHERE world_id=?', (to_iso(at + timedelta(days=3)), wid))
        source = ActionService._record_event(c, world_id=wid, tick_id=str(uuid4()), occurred_at=at, event_type='action.registration_source', actor_id=player.id, target_id=None, location_id=origin['id'], summary='隔离初始场景：编年者核对现存三座无燃料车及真实座位。', payload={})
        registry = WorldElementRegistry()
        proposal = registry.submit(c, world_id=wid, request=ElementRegistrationSubmit.model_validate({
            'source_event_id': source, 'idempotency_key': 'ride-fixture-vehicle',
            'payload': {'element_type': 'vehicle', 'name': '验收三座车', 'movement_type': 'land', 'speed_kmh': 12, 'owner_character_id': player.id, 'capacity': 3, 'location_id': origin['id']},
        }))
        registered = registry.confirm(c, world_id=wid, registration_id=proposal.id)
        assert registered.status == 'applied'
        vid = registered.result_entity_id
    return SimpleNamespace(db=db, database=db, settings=settings, clock=WorldClockService(db, settings), wid=wid, world_id=wid, at=at,
                           player=player.id, npc=npcs[0], other=npcs[1], outside=npcs[2], ids=ids, vehicle=vid, vehicle_id=vid,
                           loc=origin, origin=origin['id'], destination=destinations[0][0], sites=[d[0] for d in destinations])


def current(e, c):
    return from_iso(c.execute('SELECT w.current_time FROM worlds w WHERE w.id=?', (e.wid,)).fetchone()[0])


def choose(e, cid, operation, rid=None, **kwargs):
    with e.db.write() as c:
        actor = c.execute('SELECT * FROM characters WHERE id=?', (cid,)).fetchone()
        return RideService.apply(c, actor, current(e, c), RideIntent(operation=operation, ride_id=rid, statement='这是本人对此份真实共乘的明确选择。', **kwargs))


def offer(e, **kwargs):
    args = dict(vehicle_id=e.vehicle, member_ids=[e.player, e.npc], controller_id=e.player, destination_location_id=e.destination, deadline_world_time=e.at + timedelta(hours=2))
    args.update(kwargs)
    return choose(e, e.player, 'propose', **args)['id']


def accepted(e, **kwargs):
    rid = offer(e, **kwargs)
    members = kwargs.get('member_ids', [e.player, e.npc])
    for cid in members:
        if cid != e.player:
            choose(e, cid, 'accept', rid)
    return rid


def boarded(e, **kwargs):
    rid = accepted(e, **kwargs)
    for cid in kwargs.get('member_ids', [e.player, e.npc]):
        choose(e, cid, 'board', rid)
    return rid


def get(e, rid):
    with e.db.read() as c:
        return dict(c.execute('SELECT * FROM vehicle_rides WHERE id=?', (rid,)).fetchone())


def members(e, rid):
    with e.db.read() as c:
        return [dict(m) for m in c.execute('SELECT * FROM vehicle_ride_members WHERE ride_id=? ORDER BY ordinal', (rid,))]


def advance(e, seconds):
    with e.db.write() as c:
        before = current(e, c)
        after = before + timedelta(seconds=seconds)
        c.execute('UPDATE worlds SET current_time=? WHERE id=?', (to_iso(after), e.wid))
        MovementService().advance(c, world_id=e.wid, previous_time=before, current_time=after, created_at=utc_now())
        RideService.maintain(c, e.wid, after)


def arrival_seconds(e, rid):
    with e.db.read() as c:
        movement = c.execute('SELECT m.estimated_arrival_world FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?', (rid,)).fetchone()
        return (from_iso(movement[0]) - current(e, c)).total_seconds()


def test_each_member_accepts_and_boards_independently(ride_world):
    e = ride_world
    rid = offer(e)
    assert [m['decision'] for m in members(e, rid)] == ['accepted', 'pending']
    with e.db.read() as c:
        assert RideService.active(c, e.wid, e.npc) is None
        assert RideService.aboard(c, e.player) is None
    with pytest.raises(ValueError, match='有效接受'):
        choose(e, e.player, 'board', rid)
    result = choose(e, e.npc, 'accept', rid)
    assert result['status'] == 'accepted' and result['event_id'] != get(e, rid)['proposal_event_id']
    assert all(m['boarded_at'] is None for m in members(e, rid))
    choose(e, e.npc, 'board', rid)
    with pytest.raises(ValueError, match='每个人'):
        choose(e, e.player, 'depart', rid)
    choose(e, e.player, 'board', rid)
    choose(e, e.player, 'depart', rid)
    assert get(e, rid)['status'] == 'moving'


def test_decline_never_moves_or_overwrites_other_reply(ride_world):
    e = ride_world
    rid = offer(e, member_ids=[e.player, e.npc, e.other])
    choose(e, e.npc, 'accept', rid)
    result = choose(e, e.other, 'decline', rid)
    assert result['status'] == 'declined'
    assert [m['decision'] for m in members(e, rid)] == ['accepted', 'accepted', 'declined']
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE world_id=?', (e.wid,)).fetchone()[0] == 0
    with pytest.raises(ValueError):
        choose(e, e.other, 'accept', rid)


def test_capacity_includes_controller_and_does_not_expand(ride_world):
    e = ride_world
    with pytest.raises(ValueError, match='真实容量'):
        offer(e, member_ids=e.ids)
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM vehicle_rides').fetchone()[0] == 0
        assert c.execute('SELECT capacity FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 3


def test_unknown_old_vehicle_does_not_gain_position_or_seats(ride_world):
    e = ride_world
    with e.db.write() as c:
        c.execute('DELETE FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,))
    with pytest.raises(ValueError, match='真实位置和容量'):
        offer(e)
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM vehicle_states').fetchone()[0] == 0


def test_nonowner_cannot_drive_and_cannot_board_on_permission_of_passenger(ride_world):
    e = ride_world
    with pytest.raises(ValueError, match='驾驶使用权'):
        offer(e, controller_id=e.npc)
    rid = boarded(e)
    with pytest.raises(ValueError, match='控制者'):
        choose(e, e.npc, 'depart', rid)


@pytest.mark.parametrize('change', ['far', 'layer', 'room', 'unknown_destination'])
def test_physical_and_knowledge_boundaries_block_proposal(ride_world, change):
    e = ride_world
    with e.db.write() as c:
        if change == 'far':
            c.execute('UPDATE characters SET longitude=longitude+1 WHERE id=?', (e.npc,))
        elif change == 'layer':
            c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (e.npc,))
        elif change == 'room':
            source = c.execute('SELECT id FROM world_events WHERE world_id=? AND location_id=? LIMIT 1', (e.wid, e.origin)).fetchone()[0]
            registry = WorldElementRegistry()
            room = registry.submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
                'source_event_id': source, 'idempotency_key': 'fixture-real-room',
                'payload': {'element_type': 'interior_room', 'name': '隔离场景房间', 'location_id': e.origin, 'access_policy': 'public'},
            }))
            if room.status == 'proposed':
                room = registry.confirm(c, world_id=e.wid, registration_id=room.id)
            assert room.status == 'applied'
            c.execute('UPDATE characters SET current_room_id=? WHERE id=?', (room.result_entity_id, e.player))
        else:
            c.execute('DELETE FROM character_event_knowledge WHERE character_id=? AND event_id IN (SELECT id FROM world_events WHERE location_id=?)', (e.npc, e.destination))
    with pytest.raises(ValueError):
        offer(e)


def test_registered_vehicle_cannot_be_selected_or_driven_remotely(ride_world):
    e = ride_world
    with e.db.write() as c:
        with pytest.raises(ValueError, match='真实载具'):
            MovementService().select_transport(c, world_id=e.wid, character_id=e.player, vehicle_id=e.vehicle)
        with pytest.raises(ValueError, match='真实位置'):
            MovementService().start(c, world_id=e.wid, character_id=e.player, vehicle_id=e.vehicle, destination_longitude=e.loc['longitude'] + 0.01, destination_latitude=e.loc['latitude'], world_time=e.at)


@pytest.mark.parametrize('operation', ['start', 'cancel', 'select', 'direction'])
def test_aboard_cannot_bypass_binding_with_other_movement(ride_world, operation):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    with e.db.write() as c:
        with pytest.raises(ValueError, match='载具'):
            if operation == 'start':
                MovementService().start(c, world_id=e.wid, character_id=e.npc, destination_longitude=e.loc['longitude'] + 0.1, destination_latitude=e.loc['latitude'], world_time=e.at)
            elif operation == 'cancel':
                MovementService().cancel(c, world_id=e.wid, character_id=e.player, world_time=e.at)
            elif operation == 'select':
                MovementService().select_transport(c, world_id=e.wid, character_id=e.npc, vehicle_id=None)
            else:
                from world_engine.player_steering import PlayerSteeringService
                actor = c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone()
                PlayerSteeringService.capabilities(c, actor)


def test_one_canonical_route_projects_vehicle_and_all_occupants(ride_world):
    e = ride_world
    rid = boarded(e, member_ids=[e.player, e.npc, e.other])
    before_money = {}
    with e.db.read() as c:
        before_money = {r['id']: r['money'] for r in c.execute('SELECT id,money FROM characters WHERE world_id=?', (e.wid,))}
    choose(e, e.player, 'depart', rid)
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE world_id=? AND status='moving'", (e.wid,)).fetchone()[0] == 1
    advance(e, arrival_seconds(e, rid) / 2)
    with e.db.read() as c:
        state = c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()
        assert state['longitude'] > e.loc['longitude']
        for cid in [e.player, e.npc, e.other]:
            actor = c.execute('SELECT * FROM characters WHERE id=?', (cid,)).fetchone()
            assert (actor['longitude'], actor['latitude']) == (state['longitude'], state['latitude'])
        assert {r['id']: r['money'] for r in c.execute('SELECT id,money FROM characters WHERE world_id=?', (e.wid,))} == before_money
    advance(e, arrival_seconds(e, rid))
    assert get(e, rid)['status'] == 'arrived'
    assert all(m['disembarked_at'] is None for m in members(e, rid))
    for cid in [e.player, e.npc, e.other]:
        choose(e, cid, 'disembark', rid)
    assert get(e, rid)['status'] == 'ended'
    with e.db.read() as c:
        state = c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()
        route = c.execute('SELECT * FROM character_movements WHERE id=?', (get(e, rid)['movement_id'],)).fetchone()
        assert state['current_location_id'] == e.destination
        assert state['distance_travelled_km'] == pytest.approx(route['total_distance_km'])


def test_request_stop_preserves_occupants_until_real_safe_destination(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    advance(e, arrival_seconds(e, rid) / 2)
    result = choose(e, e.npc, 'request_stop', rid)
    assert result['stop_requested'] == 1 and result['status'] == 'moving'
    with pytest.raises(ValueError, match='行驶'):
        choose(e, e.npc, 'disembark', rid)
    with pytest.raises(ValueError, match='安全停靠'):
        choose(e, e.player, 'end', rid)
    advance(e, arrival_seconds(e, rid))
    choose(e, e.npc, 'disembark', rid)
    with e.db.read() as c:
        assert RideService.aboard(c, e.player)['id'] == rid
        assert RideService.aboard(c, e.npc) is None


def test_passenger_can_step_off_parked_vehicle_without_discarding_other_occupants(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.npc, 'disembark', rid)
    assert get(e, rid)['status'] == 'blocked'
    with e.db.read() as c:
        assert RideService.aboard(c, e.player)['id'] == rid
    with pytest.raises(ValueError):
        choose(e, e.player, 'depart', rid)
    choose(e, e.player, 'disembark', rid)
    assert get(e, rid)['status'] == 'ended'


def test_pending_invitation_does_not_bind_recipient_or_prevent_solo_movement(ride_world):
    e = ride_world
    rid = offer(e)
    with e.db.write() as c:
        actor = c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone()
        RideService.assert_available(c, actor)
        MovementService().start(c, world_id=e.wid, character_id=e.npc, destination_longitude=e.loc['longitude'] + 0.1, destination_latitude=e.loc['latitude'], world_time=e.at)
    with pytest.raises(ValueError, match='已有移动'):
        choose(e, e.npc, 'accept', rid)


def test_depart_failure_rolls_back_route_and_all_receipts(ride_world, monkeypatch):
    e = ride_world
    rid = boarded(e)
    with e.db.read() as c:
        event_count = c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
    def fail(*args, **kwargs):
        raise RuntimeError('隔离模拟后段事务失败')
    monkeypatch.setattr(RideService, '_commitments', fail)
    with pytest.raises(RuntimeError):
        choose(e, e.player, 'depart', rid)
    assert get(e, rid)['status'] == 'boarding'
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_movements').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0] == event_count
        assert c.execute('SELECT status FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 'parked'


@pytest.mark.parametrize('field', ['terms', 'decision_source', 'board_source', 'capacity'])
def test_frozen_terms_and_individual_sources_are_rechecked_at_departure(ride_world, field):
    e = ride_world
    rid = boarded(e)
    with e.db.write() as c:
        if field == 'terms':
            c.execute("UPDATE vehicle_rides SET statement='无来源的新条款' WHERE id=?", (rid,))
        elif field == 'decision_source':
            c.execute('UPDATE vehicle_ride_members SET decision_event_id=NULL WHERE ride_id=? AND character_id=?', (rid, e.npc))
        elif field == 'board_source':
            c.execute('UPDATE vehicle_ride_members SET board_event_id=NULL WHERE ride_id=? AND character_id=?', (rid, e.npc))
        else:
            c.execute('UPDATE vehicle_states SET capacity=1 WHERE vehicle_id=?', (e.vehicle,))
    with pytest.raises(ValueError):
        choose(e, e.player, 'depart', rid)


@pytest.mark.parametrize('change', ['extra', 'roster', 'naive_deadline', 'bool_capacity'])
def test_intent_schema_cannot_rewrite_reply_or_add_unapproved_resources(ride_world, change):
    e = ride_world
    if change == 'extra':
        payload = {'operation': 'accept', 'ride_id': 'a', 'statement': '本人选择', 'fuel': 100}
    elif change == 'roster':
        payload = {'operation': 'accept', 'ride_id': 'a', 'statement': '本人选择', 'member_ids': [e.npc]}
    elif change == 'naive_deadline':
        payload = {'operation': 'propose', 'vehicle_id': e.vehicle, 'member_ids': [e.player], 'controller_id': e.player, 'destination_location_id': e.destination, 'deadline_world_time': datetime(2030, 1, 1, 10), 'statement': '本人选择'}
    else:
        payload = {'operation': 'propose', 'vehicle_id': e.vehicle, 'member_ids': [e.player, e.player], 'controller_id': e.player, 'destination_location_id': e.destination, 'deadline_world_time': e.at + timedelta(hours=2), 'statement': '本人选择'}
    with pytest.raises(ValidationError):
        RideIntent.model_validate(payload)
