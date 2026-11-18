"""哥哥，同行、旅行和共乘遇到损坏路线时都必须留在真实位置。"""

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest

from test_companions import invite
from test_group_journeys import accept as accept_group, group, offer as offer_group, start as start_group
from test_journeys import choose as choose_journey, offer as offer_journey
from test_lived_world import town
from test_rides import boarded, choose as choose_ride, ride_world
from world_engine.geo import great_circle_distance_km
from world_engine.group_journeys import GroupJourneyService
from world_engine.journeys import JourneyService
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso
from world_engine.rides import RideService
from world_engine.routing import RoutePlan
from world_engine.worker import WorldWorker


def install_known_route(e, monkeypatch):
    """隔离世界中提供可核对折线，让公开出发入口真实创建绑定路线。"""
    dataset_id = str(uuid4())
    with e.db.write() as c:
        c.execute(
            "INSERT INTO navigation_datasets(id,world_id,name,asset_root,source_sha256,review_status,bounds_json,created_at,approved_at) "
            "VALUES (?,?,?,'fixture','fixture','approved','{}',?,?)",
            (dataset_id, e.wid, '隔离已核验地形', to_iso(e.at), to_iso(e.at)),
        )

    def planned(self, connection, *, world_id, actor, destination_longitude,
                destination_latitude, movement_type, speed_kmh, selected_vehicle_id):
        origin = (float(actor['longitude']), float(actor['latitude']))
        destination = (float(destination_longitude), float(destination_latitude))
        distance = great_circle_distance_km(*origin, *destination)
        assert distance > 0
        return RoutePlan(
            reachable=True, origin=origin, destination=destination,
            movement_type=movement_type, distance_km=distance,
            estimated_hours=distance / speed_kmh, dataset_id=dataset_id,
            polyline=[list(origin), list(destination)],
            edges=[{'length_km': distance, 'speed_kmh': speed_kmh}],
        )

    monkeypatch.setattr(MovementService, '_plan_route', planned)
    return dataset_id


def positions(c, people):
    return {cid: tuple(c.execute('SELECT longitude,latitude FROM characters WHERE id=?', (cid,)).fetchone())
            for cid in people}


def assert_route_failures_are_not_claimed_as_voluntary(c, movement_ids):
    for movement_id in movement_ids:
        events = c.execute(
            "SELECT summary,payload_json FROM world_events WHERE event_type='action.movement_cancelled' "
            "AND json_extract(payload_json,'$.movement_id')=?", (movement_id,),
        ).fetchall()
        assert len(events) == 1
        assert '导航路线无效' in events[0]['summary']
        assert '导航路线无效' in json.loads(events[0]['payload_json'])['reason']


def advance_past_arrival(c, e, previous=None):
    MovementService().advance(c, world_id=e.wid, previous_time=previous or e.at,
                              current_time=e.at + timedelta(minutes=30), created_at=e.at)


def advance_with_worker(e, seconds, monkeypatch):
    """哥哥，让真实 worker 按事件边界推进，不把预计时间当真实抵达。"""
    real_now = datetime(2031, 1, 1, tzinfo=UTC)
    with e.db.write() as c:
        c.execute('UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?',
                  (to_iso(real_now - timedelta(seconds=seconds)), e.wid))
    monkeypatch.setattr('world_engine.worker.utc_now', lambda: real_now)
    worker = WorldWorker(e.settings)
    iterator = worker._world_heartbeats(e.wid, None)
    for _ in range(30):
        try:
            next(iterator)
        except StopIteration:
            return
    pytest.fail('隔离世界的心跳未能越过旧预计时间边界')


@pytest.mark.parametrize('corrupt_person', ['leader', 'companion'])
@pytest.mark.parametrize('damage', ['json', 'index', 'coordinate'])
@pytest.mark.parametrize('when', ['before', 'during'])
def test_corrupt_companion_route_stops_whole_bound_walk(town, monkeypatch, corrupt_person, damage, when):
    e = town
    invite(e)
    dataset_id = install_known_route(e, monkeypatch)
    with e.db.write() as c:
        MovementService().start(c, world_id=e.wid, character_id=e.player,
                                destination_longitude=e.loc['longitude'] + 0.003,
                                destination_latitude=e.loc['latitude'], world_time=e.at)
        movements = {r['character_id']: dict(r) for r in c.execute(
            'SELECT * FROM character_movements WHERE world_id=? AND status=?', (e.wid, 'moving'))}
        assert set(movements) == {e.player, e.npc}
        assert all(row['navigation_dataset_id'] == dataset_id for row in movements.values())
        previous = e.at
        if when == 'during':
            previous = e.at + timedelta(seconds=30)
            MovementService().advance(c, world_id=e.wid, previous_time=e.at,
                                      current_time=previous, created_at=previous)
            assert all(c.execute('SELECT distance_travelled_km FROM character_movements WHERE id=?',
                                 (row['id'],)).fetchone()[0] > 0 for row in movements.values())
        damaged = e.player if corrupt_person == 'leader' else e.npc
        if damage == 'json':
            c.execute("UPDATE character_movements SET route_json='{' WHERE id=?", (movements[damaged]['id'],))
        elif damage == 'index':
            c.execute('UPDATE character_movements SET route_index=1 WHERE id=?', (movements[damaged]['id'],))
        else:
            # 哥哥，这是隔离世界里已损坏的当前状态，绝不自动修正人物坐标。
            c.execute('UPDATE characters SET longitude=longitude+0.00005 WHERE id=?', (damaged,))
        before = positions(c, [e.player, e.npc])
        advance_past_arrival(c, e, previous)
        assert positions(c, [e.player, e.npc]) == before
        assert not c.execute("SELECT 1 FROM character_movements WHERE world_id=? AND status IN ('moving','arrived')", (e.wid,)).fetchone()
        assert not c.execute("SELECT 1 FROM world_events WHERE world_id=? AND event_type='action.movement_arrived'", (e.wid,)).fetchone()
        assert_route_failures_are_not_claimed_as_voluntary(c, [row['id'] for row in movements.values()])


@pytest.mark.parametrize('corrupt_person', ['player', 'npc'])
def test_corrupt_route_pauses_two_person_journey_without_false_arrival(town, monkeypatch, corrupt_person):
    e = town
    jid = offer_journey(e)
    assert choose_journey(e, 'accept', jid).status_code == 200
    install_known_route(e, monkeypatch)
    assert choose_journey(e, 'depart', jid).status_code == 200
    with e.db.write() as c:
        before = positions(c, [e.player, e.npc])
        leg = c.execute("SELECT * FROM journey_legs WHERE journey_id=? AND status='moving'", (jid,)).fetchone()
        assert leg
        damaged = leg['player_movement_id'] if corrupt_person == 'player' else leg['npc_movement_id']
        c.execute("UPDATE character_movements SET route_json='{' WHERE id=?", (damaged,))
        advance_past_arrival(c, e)
        JourneyService.maintain(c, e.wid, e.at + timedelta(minutes=30))
        assert positions(c, [e.player, e.npc]) == before
        assert c.execute('SELECT status FROM joint_journeys WHERE id=?', (jid,)).fetchone()[0] == 'paused'
        assert c.execute('SELECT status FROM journey_legs WHERE id=?', (leg['id'],)).fetchone()[0] == 'cancelled'
        assert not c.execute('SELECT 1 FROM journey_stops WHERE journey_id=? AND arrived_at IS NOT NULL', (jid,)).fetchone()
        assert_route_failures_are_not_claimed_as_voluntary(c, [leg['player_movement_id'], leg['npc_movement_id']])


def test_corrupt_group_route_pauses_everyone_without_false_arrival(group, monkeypatch):
    e = group
    jid = accept_group(e, offer_group(e))
    install_known_route(e, monkeypatch)
    start_group(e, jid)
    with e.db.write() as c:
        before = positions(c, e.ids)
        leg = c.execute("SELECT * FROM group_journey_legs WHERE journey_id=? AND status='moving'", (jid,)).fetchone()
        assert leg
        damaged = c.execute('SELECT movement_id FROM group_journey_leg_members WHERE leg_id=? AND character_id=?', (leg['id'], e.npc)).fetchone()[0]
        c.execute("UPDATE character_movements SET route_json='{' WHERE id=?", (damaged,))
        advance_past_arrival(c, e)
        GroupJourneyService.maintain(c, e.wid, e.at + timedelta(minutes=30))
        assert positions(c, e.ids) == before
        assert c.execute('SELECT status FROM group_journeys WHERE id=?', (jid,)).fetchone()[0] == 'paused'
        assert c.execute('SELECT status FROM group_journey_legs WHERE id=?', (leg['id'],)).fetchone()[0] == 'cancelled'
        assert not c.execute('SELECT 1 FROM group_journey_stops WHERE journey_id=? AND arrived_at IS NOT NULL', (jid,)).fetchone()
        ids = [row[0] for row in c.execute('SELECT movement_id FROM group_journey_leg_members WHERE leg_id=?', (leg['id'],))]
        assert_route_failures_are_not_claimed_as_voluntary(c, ids)


def test_corrupt_ride_route_preserves_vehicle_and_all_boarded_people(ride_world, monkeypatch):
    e = ride_world
    rid = boarded(e, member_ids=[e.player, e.npc, e.other])
    install_known_route(e, monkeypatch)
    choose_ride(e, e.player, 'depart', rid)
    with e.db.write() as c:
        before = positions(c, [e.player, e.npc, e.other])
        vehicle = tuple(c.execute('SELECT longitude,latitude,distance_travelled_km FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone())
        movement = c.execute('SELECT movement_id FROM vehicle_rides WHERE id=?', (rid,)).fetchone()[0]
        c.execute("UPDATE character_movements SET route_json='{' WHERE id=?", (movement,))
        advance_past_arrival(c, e)
        RideService.maintain(c, e.wid, e.at + timedelta(minutes=30))
        assert positions(c, [e.player, e.npc, e.other]) == before
        assert tuple(c.execute('SELECT longitude,latitude,distance_travelled_km FROM vehicle_states WHERE vehicle_id=?', (e.vehicle,)).fetchone()) == vehicle
        assert c.execute('SELECT status FROM vehicle_rides WHERE id=?', (rid,)).fetchone()[0] == 'blocked'
        assert c.execute('SELECT status FROM character_movements WHERE id=?', (movement,)).fetchone()[0] == 'suspended'
        assert c.execute('SELECT COUNT(*) FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL', (rid,)).fetchone()[0] == 3
        assert not c.execute("SELECT 1 FROM world_events WHERE world_id=? AND event_type='action.movement_arrived'", (e.wid,)).fetchone()


@pytest.mark.parametrize('eta_offset', [-5, 5])
def test_companion_old_eta_never_overrides_actual_travel(town, monkeypatch, eta_offset):
    e = town
    invite(e)
    install_known_route(e, monkeypatch)
    with e.db.write() as c:
        move = MovementService().start(c, world_id=e.wid, character_id=e.player,
                                       destination_longitude=e.loc['longitude'] + 0.003,
                                       destination_latitude=e.loc['latitude'], world_time=e.at)
        eta = move.estimated_arrival_world
        assert eta > e.at + timedelta(seconds=7)
        c.execute('UPDATE character_movements SET estimated_arrival_world=? WHERE world_id=? AND status=?',
                  (to_iso(eta + timedelta(seconds=eta_offset)), e.wid, 'moving'))
    seconds = (eta - e.at).total_seconds() + (-2 if eta_offset < 0 else 2)
    advance_with_worker(e, seconds, monkeypatch)
    with e.db.read() as c:
        rows = c.execute('SELECT status,longitude,destination_longitude FROM character_movements m '
                         'JOIN characters c ON c.id=m.character_id WHERE m.world_id=?', (e.wid,)).fetchall()
        assert len(rows) == 2
        if eta_offset < 0:
            assert all(row['status'] == 'moving' and row['longitude'] != row['destination_longitude'] for row in rows)
            assert not c.execute("SELECT 1 FROM world_events WHERE world_id=? AND event_type='action.movement_arrived'", (e.wid,)).fetchone()
        else:
            assert all(row['status'] == 'arrived' and row['longitude'] == pytest.approx(row['destination_longitude']) for row in rows)


@pytest.mark.parametrize('eta_offset', [-5, 5])
def test_shared_ride_old_eta_never_overrides_actual_travel(ride_world, monkeypatch, eta_offset):
    e = ride_world
    rid = boarded(e, member_ids=[e.player, e.npc, e.other])
    install_known_route(e, monkeypatch)
    choose_ride(e, e.player, 'depart', rid)
    with e.db.write() as c:
        movement = c.execute('SELECT m.* FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?', (rid,)).fetchone()
        eta = from_iso(movement['estimated_arrival_world'])
        assert eta > e.at + timedelta(seconds=7)
        c.execute('UPDATE character_movements SET estimated_arrival_world=? WHERE id=?',
                  (to_iso(eta + timedelta(seconds=eta_offset)), movement['id']))
    seconds = (eta - e.at).total_seconds() + (-2 if eta_offset < 0 else 2)
    advance_with_worker(e, seconds, monkeypatch)
    with e.db.read() as c:
        row = c.execute('SELECT * FROM vehicle_rides WHERE id=?', (rid,)).fetchone()
        if eta_offset < 0:
            assert row['status'] == 'moving' and row['arrived_at'] is None
            assert c.execute('SELECT status FROM character_movements WHERE id=?', (movement['id'],)).fetchone()[0] == 'moving'
        else:
            assert row['status'] == 'arrived' and row['arrived_at'] is not None
            assert c.execute('SELECT status FROM character_movements WHERE id=?', (movement['id'],)).fetchone()[0] == 'arrived'
        assert c.execute('SELECT COUNT(*) FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL', (rid,)).fetchone()[0] == 3


def slow_real_routes(c, world_id):
    """隔离测试模拟出发后实际路段降速，但不改写任何人的同意期限。"""
    for row in c.execute("SELECT id,route_json FROM character_movements WHERE world_id=? AND status='moving'", (world_id,)).fetchall():
        route = json.loads(row['route_json'])
        for edge in route['edges']:
            edge['speed_kmh'] = 0.1
        c.execute('UPDATE character_movements SET route_json=? WHERE id=?', (json.dumps(route), row['id']))


def test_companion_expiry_still_stops_follower_with_slow_route(town, monkeypatch):
    e = town
    session = invite(e, minutes=5)
    install_known_route(e, monkeypatch)
    with e.db.write() as c:
        MovementService().start(c, world_id=e.wid, character_id=e.player,
                                destination_longitude=e.loc['longitude'] + 0.001,
                                destination_latitude=e.loc['latitude'], world_time=e.at)
        slow_real_routes(c, e.wid)
    advance_with_worker(e, 305, monkeypatch)
    with e.db.read() as c:
        assert c.execute('SELECT status FROM companion_sessions WHERE id=?', (session['id'],)).fetchone()[0] == 'ended'
        follower = c.execute('SELECT * FROM character_movements WHERE world_id=? AND character_id=?', (e.wid, e.npc)).fetchone()
        assert follower['status'] == 'cancelled'
        assert 0 < follower['distance_travelled_km'] <= 0.1 * 5 / 60 + 0.00001
        assert not c.execute("SELECT 1 FROM world_events WHERE world_id=? AND event_type='action.movement_arrived'", (e.wid,)).fetchone()


def test_ride_expiry_preserves_boarded_people_with_slow_route(ride_world, monkeypatch):
    e = ride_world
    with e.db.write() as c:
        c.execute('UPDATE locations SET longitude=?,latitude=? WHERE id=?',
                  (e.loc['longitude'] + 0.003, e.loc['latitude'], e.destination))
    rid = boarded(e, member_ids=[e.player, e.npc, e.other], deadline_world_time=e.at + timedelta(minutes=5))
    install_known_route(e, monkeypatch)
    choose_ride(e, e.player, 'depart', rid)
    with e.db.write() as c:
        slow_real_routes(c, e.wid)
    advance_with_worker(e, 305, monkeypatch)
    with e.db.read() as c:
        assert c.execute('SELECT status FROM vehicle_rides WHERE id=?', (rid,)).fetchone()[0] == 'blocked'
        movement = c.execute('SELECT m.* FROM character_movements m JOIN vehicle_rides r ON r.movement_id=m.id WHERE r.id=?', (rid,)).fetchone()
        assert movement['status'] == 'suspended'
        assert 0 < movement['distance_travelled_km'] <= 0.1 * 5 / 60 + 0.00001
        assert c.execute('SELECT COUNT(*) FROM vehicle_ride_members WHERE ride_id=? AND boarded_at IS NOT NULL AND disembarked_at IS NULL', (rid,)).fetchone()[0] == 3
        assert not c.execute("SELECT 1 FROM world_events WHERE world_id=? AND event_type='action.movement_arrived' "
                             "AND json_extract(payload_json,'$.movement_id')=?", (e.wid, movement['id'])).fetchone()
