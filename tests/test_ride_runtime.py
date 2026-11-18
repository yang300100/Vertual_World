"""哥哥，这里核验真实时钟、重启、租约终止和离车后的知识边界。"""

from datetime import timedelta
import json
from uuid import uuid4

import pytest

from test_rides import accepted, advance, arrival_seconds, boarded, choose, current, get, members, offer
from test_rides import ride_world  # noqa: F401
from world_engine.contracts import ContractService
from world_engine.database import Database
from world_engine.movement import MovementService
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import to_iso, utc_now
from world_engine.rides import RideService


def rental(e, *, seconds=3600):
    with e.db.write() as c:
        source = c.execute("SELECT id FROM world_events WHERE world_id=? AND location_id=? LIMIT 1", (e.wid, e.origin)).fetchone()[0]
        registry = WorldElementRegistry()
        proposal = registry.submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
            'source_event_id': source, 'idempotency_key': 'rental-vehicle-' + str(uuid4()),
            'payload': {'element_type': 'vehicle', 'name': '隔离租用三座车', 'movement_type': 'land', 'speed_kmh': 12, 'capacity': 3, 'location_id': e.origin, 'owner_character_id': e.npc},
        }))
        result = registry.confirm(c, world_id=e.wid, registration_id=proposal.id)
        assert result.status == 'applied'
        vid = result.result_entity_id
        terms = {'vehicle_id': vid, 'payment': 5, 'duration_days': 1}
        request_id = str(uuid4())
        c.execute("INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,terms_json,status,created_at,updated_at) VALUES (?,?,?,?,'lease',?,'npc_accepted',?,?)", (request_id, e.wid, e.player, e.npc, json.dumps(terms), to_iso(utc_now()), to_iso(utc_now())))
        request = c.execute('SELECT * FROM long_term_operation_requests WHERE id=?', (request_id,)).fetchone()
        # 哥哥，租约开始时间是隔离场景的初始历史，付款仍经过真实合同规则。
        started = e.at - timedelta(days=1) + timedelta(seconds=seconds)
        ContractService.start(c, request, terms, to_iso(started))
    e.vehicle = e.vehicle_id = vid
    return request_id


def test_actual_clock_arrives_with_one_route_and_keeps_passengers_aboard(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    e.clock.heartbeat(e.wid, elapsed_seconds=arrival_seconds(e, rid) + 2)
    assert get(e, rid)['status'] == 'arrived'
    with e.db.read() as c:
        state = c.execute('SELECT * FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()
        for cid in [e.player, e.npc]:
            actor = c.execute('SELECT * FROM characters WHERE id=?', (cid,)).fetchone()
            assert actor['current_location_id'] == e.destination
            assert (actor['longitude'], actor['latitude']) == (state['longitude'], state['latitude'])
            assert RideService.aboard(c, cid)['id'] == rid
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE world_id=? AND vehicle_id=?", (e.wid, e.vehicle)).fetchone()[0] == 1


def test_restart_does_not_duplicate_route_or_forget_occupancy(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    advance(e, arrival_seconds(e, rid) / 2)
    old = get(e, rid)
    reopened = Database(e.settings.database_path)
    reopened.initialize()
    e.db = e.database = reopened
    assert get(e, rid) == old
    with reopened.read() as c:
        assert RideService.aboard(c, e.npc)['id'] == rid
        assert c.execute("SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?", (e.vehicle,)).fetchone()[0] == 1
    advance(e, arrival_seconds(e, rid))
    assert get(e, rid)['status'] == 'arrived'


def test_board_and_depart_retries_do_not_add_receipts_or_routes(ride_world):
    e = ride_world
    rid = boarded(e)
    with e.db.read() as c:
        events = c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
    original_members = {m['character_id']: m for m in members(e, rid)}
    assert choose(e, e.npc, 'board', rid)['event_id'] == original_members[e.npc]['board_event_id']
    assert choose(e, e.player, 'board', rid)['event_id'] == original_members[e.player]['board_event_id']
    assert choose(e, e.npc, 'accept', rid)['event_id'] == original_members[e.npc]['decision_event_id']
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0] == events
    departure = choose(e, e.player, 'depart', rid)['event_id']
    stop_npc = choose(e, e.npc, 'request_stop', rid)['event_id']
    stop_player = choose(e, e.player, 'request_stop', rid)['event_id']
    assert stop_npc != stop_player
    assert choose(e, e.npc, 'request_stop', rid)['event_id'] == stop_npc
    assert choose(e, e.player, 'depart', rid)['event_id'] == departure
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 1


def test_original_roster_reserves_last_seat_and_outsider_cannot_board(ride_world):
    e = ride_world
    rid = accepted(e, member_ids=[e.player, e.npc, e.other])
    choose(e, e.player, 'board', rid)
    choose(e, e.npc, 'board', rid)
    with pytest.raises(ValueError, match='不是'):
        choose(e, e.outside, 'board', rid)
    choose(e, e.other, 'board', rid)
    assert sum(bool(m['boarded_at']) for m in members(e, rid)) == 3


def test_pending_recipient_can_accept_another_own_offer(ride_world):
    e = ride_world
    first = offer(e)
    with e.db.write() as c:
        source = c.execute('SELECT id FROM world_events WHERE world_id=? AND location_id=? LIMIT 1', (e.wid, e.origin)).fetchone()[0]
        registry = WorldElementRegistry()
        proposal = registry.submit(c, world_id=e.wid, request=ElementRegistrationSubmit.model_validate({
            'source_event_id': source, 'idempotency_key': 'independent-recipient-vehicle',
            'payload': {'element_type': 'vehicle', 'name': 'NPC本人的三座车', 'movement_type': 'land', 'speed_kmh': 12, 'capacity': 3, 'location_id': e.origin, 'owner_character_id': e.npc},
        }))
        own = registry.confirm(c, world_id=e.wid, registration_id=proposal.id).result_entity_id
    result = choose(e, e.npc, 'propose', vehicle_id=own, member_ids=[e.npc, e.other], controller_id=e.npc, destination_location_id=e.destination, deadline_world_time=e.at + timedelta(hours=1))
    assert result['status'] == 'pending'
    with pytest.raises(ValueError, match='另一份'):
        choose(e, e.npc, 'accept', first)


def test_pending_recipient_who_walks_away_does_not_receive_remote_live_consent(ride_world):
    e = ride_world
    rid = offer(e, member_ids=[e.player, e.npc, e.other])
    with e.db.write() as c:
        MovementService().start(c, world_id=e.wid, character_id=e.npc,
                                destination_longitude=e.loc['longitude'] + 0.1,
                                destination_latitude=e.loc['latitude'], world_time=e.at)
    advance(e, 60)
    reply = choose(e, e.other, 'accept', rid)
    with e.db.read() as c:
        assert c.execute('SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?', (e.npc, reply['event_id'])).fetchone() is None
        person = c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone()
        viewed = RideService.rows(c, person, current(e, c))[0]
        other = next(m for m in viewed['members'] if m['character_id'] == e.other)
        assert other['decision'] == 'pending'
        assert other['decision_event_id'] is None


def test_insufficient_rental_duration_rolls_back_departure(ride_world):
    e = ride_world
    rental(e, seconds=30)
    rid = boarded(e)
    with pytest.raises(ValueError, match='租用期限'):
        choose(e, e.player, 'depart', rid)
    assert get(e, rid)['status'] == 'boarding'
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 0
        assert RideService.aboard(c, e.npc)['id'] == rid


def test_real_contract_cancellation_suspends_in_place_without_discarding_passengers(ride_world):
    e = ride_world
    request_id = rental(e)
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    advance(e, arrival_seconds(e, rid) / 2)
    with e.db.write() as c:
        position = tuple(c.execute('SELECT longitude,latitude FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone())
        contract = c.execute('SELECT * FROM contract_fulfillments WHERE request_id=?', (request_id,)).fetchone()
        ContractService.cancel(c, contract, to_iso(current(e, c)))
        assert c.execute('SELECT status FROM character_movements WHERE id=?', (get(e, rid)['movement_id'],)).fetchone()[0] == 'suspended'
    assert get(e, rid)['status'] == 'blocked'
    advance(e, 120)
    with e.db.read() as c:
        assert tuple(c.execute('SELECT longitude,latitude FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()) == position
        assert RideService.aboard(c, e.npc)['id'] == rid
        assert RideService.aboard(c, e.player)['id'] == rid
    with pytest.raises(ValueError, match='站点'):
        choose(e, e.npc, 'disembark', rid)


def test_expired_parked_rental_keeps_people_until_each_safe_disembark(ride_world):
    e = ride_world
    request_id = rental(e, seconds=30)
    rid = boarded(e)
    with e.db.write() as c:
        at = e.at + timedelta(seconds=30)
        c.execute('UPDATE worlds SET current_time=? WHERE id=?', (to_iso(at), e.wid))
        assert ContractService.advance(c, e.wid, to_iso(at)) == 1
        assert c.execute('SELECT status FROM contract_fulfillments WHERE request_id=?', (request_id,)).fetchone()[0] == 'expired'
        assert RideService.aboard(c, e.npc)['id'] == rid
    choose(e, e.npc, 'disembark', rid)
    choose(e, e.player, 'disembark', rid)
    assert get(e, rid)['status'] == 'ended'


@pytest.mark.parametrize('cause', ['unavailable', 'owner_changed', 'driver_health'])
def test_invalidated_driver_or_vehicle_cannot_travel_before_blocking(ride_world, cause):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    with e.db.write() as c:
        if cause == 'unavailable':
            c.execute('UPDATE vehicles SET is_available=0 WHERE id=?', (e.vehicle,))
        elif cause == 'owner_changed':
            c.execute('UPDATE vehicles SET owner_character_id=? WHERE id=?', (e.other, e.vehicle))
        else:
            c.execute('UPDATE characters SET health=0 WHERE id=?', (e.player,))
    advance(e, 30)
    assert get(e, rid)['status'] == 'blocked'
    with e.db.read() as c:
        assert c.execute('SELECT distance_travelled_km FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 0
        assert RideService.aboard(c, e.npc)['id'] == rid


def test_passenger_arrival_is_a_real_participant_location_source(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    advance(e, arrival_seconds(e, rid))
    with e.db.read() as c:
        event = c.execute("SELECT e.* FROM world_events e JOIN event_observers o ON o.event_id=e.id WHERE e.world_id=? AND e.event_type='social.ride_arrived' AND o.character_id=? AND o.channel='participant'", (e.wid, e.npc)).fetchone()
        assert event['location_id'] == e.destination
        assert json.loads(event['payload_json'])['movement_id'] == get(e, rid)['movement_id']


def test_departed_member_does_not_receive_later_progress_or_track_live_vehicle(ride_world):
    e = ride_world
    rid = boarded(e)
    choose(e, e.player, 'depart', rid)
    advance(e, arrival_seconds(e, rid))
    choose(e, e.npc, 'disembark', rid)
    with e.db.read() as c:
        old_rows = RideService.rows(c, c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone(), current(e, c))
        assert 'longitude' not in old_rows[0]['vehicle']
        knowledge = c.execute('SELECT COUNT(*) FROM character_event_knowledge WHERE character_id=?', (e.npc,)).fetchone()[0]
    choose(e, e.player, 'disembark', rid)
    with e.db.read() as c:
        assert c.execute('SELECT COUNT(*) FROM character_event_knowledge WHERE character_id=?', (e.npc,)).fetchone()[0] == knowledge
        history = RideService.rows(c, c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone(), current(e, c))[0]
        player = next(m for m in history['members'] if m['character_id'] == e.player)
        assert player['disembarked_at'] is None


def test_no_automatic_fuel_or_free_transport_for_finite_fuel_definition(ride_world):
    e = ride_world
    with e.db.write() as c:
        c.execute('UPDATE vehicles SET metadata_json=? WHERE id=?', (json.dumps({'fuel': 3, 'fuel_per_km': 1}), e.vehicle))
    with pytest.raises(ValueError, match='燃料'):
        offer(e)
    with e.db.read() as c:
        assert json.loads(c.execute('SELECT metadata_json FROM vehicles WHERE id=?', (e.vehicle,)).fetchone()[0]) == {'fuel': 3, 'fuel_per_km': 1}
        assert c.execute('SELECT COUNT(*) FROM character_movements WHERE vehicle_id=?', (e.vehicle,)).fetchone()[0] == 0
