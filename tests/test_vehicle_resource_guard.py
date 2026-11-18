"""哥哥，旧单人载具的资源门禁只在隔离世界验收。"""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from test_rides import ride_world  # noqa: F401
from world_engine.movement import MovementService
from world_engine.player_steering import PlayerSteeringService
from world_engine.repository import to_iso, utc_now


@pytest.fixture(params=[{'fuel': 0}, {'FuEl_Per_Km': 1}, {'燃料': 3}], ids=['empty', 'mixed_case', 'chinese'])
def legacy_fuel_vehicle(ride_world, request):
    e=ride_world;vid=str(uuid4());stamp=to_iso(utc_now())
    with e.db.write() as c:
        c.execute("INSERT INTO vehicles(id,world_id,name,movement_type,speed_kmh,owner_character_id,is_available,metadata_json,created_at,updated_at) VALUES (?,?,?,'land',12,?,1,?,?,?)",
            (vid,e.wid,'隔离旧载具',e.player,json.dumps(request.param,ensure_ascii=False),stamp,stamp))
    e.legacy_vehicle=vid
    return e


def snapshot(c,e):
    return {
        'player':dict(c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()),
        'vehicle':dict(c.execute('SELECT * FROM vehicles WHERE id=?',(e.legacy_vehicle,)).fetchone()),
        'movements':[dict(row) for row in c.execute('SELECT * FROM character_movements WHERE world_id=? ORDER BY rowid',(e.wid,))],
        'steering':[dict(row) for row in c.execute('SELECT * FROM player_steering WHERE world_id=? ORDER BY rowid',(e.wid,))],
        'events':c.execute('SELECT COUNT(*) FROM world_events WHERE world_id=?',(e.wid,)).fetchone()[0],
        'contracts':[dict(row) for row in c.execute('SELECT * FROM contract_fulfillments WHERE world_id=? ORDER BY rowid',(e.wid,))],
    }


def start(c,e,vehicle_id):
    return MovementService().start(c,world_id=e.wid,character_id=e.player,
        destination_longitude=e.loc['longitude']+0.01,destination_latitude=e.loc['latitude'],
        vehicle_id=vehicle_id,world_time=e.at)


def test_select_rejects_finite_fuel_without_changing_any_state(legacy_fuel_vehicle):
    e=legacy_fuel_vehicle
    with e.db.write() as c:
        before=snapshot(c,e)
        for _ in range(2):
            with pytest.raises(ValueError,match='燃料'):
                MovementService().select_transport(c,world_id=e.wid,character_id=e.player,vehicle_id=e.legacy_vehicle)
            assert snapshot(c,e)==before


@pytest.mark.parametrize('selected',[False,True],ids=['explicit_vehicle','already_selected'])
def test_destination_rejects_without_changing_any_state(legacy_fuel_vehicle,selected):
    e=legacy_fuel_vehicle
    with e.db.write() as c:
        if selected:
            # 哥哥，这是升级前已经选择交通工具的旧存档初始状态，不是在拒绝时补状态。
            c.execute("UPDATE characters SET active_vehicle_id=?,movement_type='land',movement_speed_kmh=12 WHERE id=?",(e.legacy_vehicle,e.player))
        before=snapshot(c,e)
        for _ in range(2):
            with pytest.raises(ValueError,match='燃料'):
                start(c,e,None if selected else e.legacy_vehicle)
            assert snapshot(c,e)==before


def test_direction_rejects_without_changing_any_state(legacy_fuel_vehicle):
    e=legacy_fuel_vehicle
    with e.db.write() as c:
        c.execute("UPDATE characters SET active_vehicle_id=?,movement_type='land',movement_speed_kmh=12 WHERE id=?",(e.legacy_vehicle,e.player))
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
        before=snapshot(c,e)
        for seq in (1,2):
            with pytest.raises(ValueError,match='燃料'):
                PlayerSteeringService.set_direction(c,actor,(1,0),seq,e.at,utc_now())
            assert snapshot(c,e)==before


@pytest.mark.parametrize('metadata',[{}, {'color':'blue','description':'篮子里没有备用燃料'}])
def test_ordinary_metadata_retains_existing_selection_and_movement(ride_world,metadata):
    e=ride_world;vid=str(uuid4());stamp=to_iso(utc_now());e.legacy_vehicle=vid
    with e.db.write() as c:
        c.execute("INSERT INTO vehicles(id,world_id,name,movement_type,speed_kmh,owner_character_id,is_available,metadata_json,created_at,updated_at) VALUES (?,?,?,'land',12,?,1,?,?,?)",
            (vid,e.wid,'普通旧载具',e.player,json.dumps(metadata,ensure_ascii=False),stamp,stamp))
        MovementService().select_transport(c,world_id=e.wid,character_id=e.player,vehicle_id=vid)
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
        assert actor['active_vehicle_id']==vid and actor['movement_speed_kmh']==12
        assert PlayerSteeringService.capabilities(c,actor)[:2]==(12,'land')
        movement=start(c,e,None)
        assert movement.vehicle_id==vid and movement.speed_kmh==12


def test_direct_failed_departure_does_not_stop_existing_direction(legacy_fuel_vehicle):
    e=legacy_fuel_vehicle
    with e.db.write() as c:
        original=c.execute('SELECT metadata_json FROM vehicles WHERE id=?',(e.legacy_vehicle,)).fetchone()[0]
        c.execute("UPDATE vehicles SET metadata_json='{}' WHERE id=?",(e.legacy_vehicle,))
        MovementService().select_transport(c,world_id=e.wid,character_id=e.player,vehicle_id=e.legacy_vehicle)
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
        PlayerSteeringService.set_direction(c,actor,(1,0),1,e.at,utc_now())
        c.execute('UPDATE vehicles SET metadata_json=? WHERE id=?',(original,e.legacy_vehicle))
        before=snapshot(c,e)
        with pytest.raises(ValueError,match='燃料'):
            MovementService()._start_solo(c,world_id=e.wid,character_id=e.player,
                destination_longitude=e.loc['longitude']+0.01,destination_latitude=e.loc['latitude'],world_time=e.at)
        assert snapshot(c,e)==before and PlayerSteeringService.active(c,e.player)


@pytest.mark.parametrize('operation',['select','destination','direction'])
def test_foreign_vehicle_does_not_disclose_its_resource_metadata(legacy_fuel_vehicle,operation):
    e=legacy_fuel_vehicle
    with e.db.write() as c:
        c.execute('UPDATE vehicles SET owner_character_id=? WHERE id=?',(e.npc,e.legacy_vehicle))
        if operation=='direction':
            c.execute('UPDATE characters SET active_vehicle_id=? WHERE id=?',(e.legacy_vehicle,e.player))
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
        before=snapshot(c,e)
        with pytest.raises(ValueError,match='不可用|不属于') as error:
            if operation=='select':MovementService().select_transport(c,world_id=e.wid,character_id=e.player,vehicle_id=e.legacy_vehicle)
            elif operation=='destination':start(c,e,e.legacy_vehicle)
            else:PlayerSteeringService.set_direction(c,actor,(1,0),1,e.at,utc_now())
        assert '燃料' not in str(error.value)
        assert snapshot(c,e)==before


@pytest.mark.parametrize('metadata',[{'fuel_description':'人力车辆的说明'}, {'fuel':3}],ids=['description_only','numeric_field'])
@pytest.mark.parametrize('kind',['direction','route'])
def test_existing_legacy_motion_is_not_reinterpreted_as_a_new_start(ride_world,metadata,kind):
    e=ride_world;vid=str(uuid4());stamp=to_iso(utc_now());e.legacy_vehicle=vid
    with e.db.write() as c:
        c.execute("INSERT INTO vehicles(id,world_id,name,movement_type,speed_kmh,owner_character_id,is_available,metadata_json,created_at,updated_at) VALUES (?,?,?,'land',12,?,1,'{}',?,?)",
            (vid,e.wid,'既有旧行程载具',e.player,stamp,stamp))
        MovementService().select_transport(c,world_id=e.wid,character_id=e.player,vehicle_id=vid)
        wall=utc_now()
        if kind=='direction':
            actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
            PlayerSteeringService.set_direction(c,actor,(1,0),1,e.at,wall)
        else:
            movement=start(c,e,None)
        # 哥哥，以下是旧存档的明确初始状态；该测试不在心跳时改变资源定义。
        c.execute('UPDATE vehicles SET metadata_json=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),vid))
        before=snapshot(c,e)
        if kind=='direction':
            actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
            with pytest.raises(ValueError,match='燃料'):
                PlayerSteeringService.set_direction(c,actor,(1,0),2,e.at,wall)
            assert snapshot(c,e)==before
            assert PlayerSteeringService.advance(c,e.wid,e.at+timedelta(seconds=1),wall+timedelta(seconds=1))>0
            assert PlayerSteeringService.active(c,e.player)
            # 哥哥，本人仍可松开方向键，停止操作不需要新的燃料许可。
            actor=c.execute('SELECT * FROM characters WHERE id=?',(e.player,)).fetchone()
            PlayerSteeringService.set_direction(c,actor,(0,0),3,e.at+timedelta(seconds=1),wall+timedelta(seconds=1))
            assert not PlayerSteeringService.active(c,e.player)
        else:
            MovementService().advance(c,world_id=e.wid,previous_time=e.at,current_time=e.at+timedelta(seconds=1),created_at=wall+timedelta(seconds=1))
            row=c.execute('SELECT * FROM character_movements WHERE id=?',(movement.id,)).fetchone()
            assert row['status']=='moving' and row['distance_travelled_km']>0
        after=snapshot(c,e)
        assert after['player']['longitude']>before['player']['longitude']
        assert after['player']['money']==before['player']['money']
        assert json.loads(after['vehicle']['metadata_json'])==metadata
