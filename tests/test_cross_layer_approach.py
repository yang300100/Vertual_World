"""哥哥，两人分别真实走到通道，不以靠近作为通过同意。"""

from datetime import timedelta
import json

import pytest

from test_cross_layer import prepared, at, talk  # noqa: F401
from test_wonders import wonder_world  # noqa: F401
from world_engine.cross_layer import CrossLayerPassageService as Passage
from world_engine.geo import great_circle_distance_km
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso, utc_now


def offset(c,e):
    # 哥哥，只在独立测试设定通道附近的两人位置，两人之间仍可现场对话。
    city=e['city']
    for actor, delta in ((e['player'],0.00045),(e['first'],0.00040)):
        c.execute('UPDATE characters SET longitude=?,latitude=? WHERE id=?',
                  (city['longitude']+delta,city['latitude'],actor))


def propose(c,e,key='invitation'):
    return Passage.propose(c,e['wid'],e['player'],e['first'],e['city']['id'],'enter',key,at(c,e))


def arrive(c,e,prev,move_ids):
    end=max(from_iso(c.execute('SELECT estimated_arrival_world FROM character_movements WHERE id=?',(mid,)).fetchone()[0]) for mid in move_ids)
    end+=timedelta(seconds=1)
    MovementService().advance(c,world_id=e['wid'],previous_time=prev,current_time=end,created_at=utc_now())
    c.execute('UPDATE worlds SET current_time=? WHERE id=?',(to_iso(end),e['wid']))
    for mid in move_ids:
        assert c.execute('SELECT status FROM character_movements WHERE id=?',(mid,)).fetchone()[0]=='arrived'
    return end


def test_each_person_walks_then_npc_decides_again_before_passage(prepared):
    e=prepared
    with e['db'].write() as c:
        offset(c,e)
        before={cid:dict(c.execute('SELECT * FROM characters WHERE id=?',(cid,)).fetchone()) for cid in (e['player'],e['first'])}
        assert e['city']['id'] in [w['id'] for w in Passage.options(c,e['wid'],e['player'],at(c,e))['wonders']]
        p=propose(c,e)
        context=Passage.npc_context(c,e['wid'],e['first'],e['player'],at(c,e))[0]
        assert not context['npc_at_node'] and not context['player_at_node'] and context['npc_can_approach']
        source=talk(c,e)
        assert source.accepted
        prev=at(c,e)
        npc=Passage.npc_decide(c,e['wid'],e['first'],p['id'],'approach',prev,source.event_id)
        assert npc['status']=='pending' and npc['approach_status']=='applied'
        repeat=Passage.npc_decide(c,e['wid'],e['first'],p['id'],'approach',prev,source.event_id)
        assert repeat['approach_status']=='already' and repeat['movement_id']==npc['movement_id']
        player=Passage.player_approach(c,e['wid'],e['player'],p['id'],'walk-one',prev)
        assert player['approach_status']=='applied'
        again=Passage.player_approach(c,e['wid'],e['player'],p['id'],'walk-one',prev)
        assert again['approach_status']=='already' and again['movement_id']==player['movement_id']
        assert all(c.execute('SELECT space_layer FROM characters WHERE id=?',(cid,)).fetchone()[0]=='ground' for cid in before)
        assert all(dict(c.execute('SELECT * FROM characters WHERE id=?',(cid,)).fetchone())['longitude']==before[cid]['longitude'] for cid in before)
        arrive(c,e,prev,[npc['movement_id'],player['movement_id']])
        for cid in before:
            actor=c.execute('SELECT * FROM characters WHERE id=?',(cid,)).fetchone()
            assert great_circle_distance_km(actor['longitude'],actor['latitude'],e['city']['longitude'],e['city']['latitude']) <= Passage.NODE_RADIUS_KM
            assert actor['space_layer']=='ground'
        assert c.execute('SELECT status FROM cross_layer_passages WHERE id=?',(p['id'],)).fetchone()[0]=='pending'
        with pytest.raises(ValueError,match='一次现场回应'):
            Passage.npc_decide(c,e['wid'],e['first'],p['id'],'enter',at(c,e),source.event_id)
        new_source=talk(c,e)
        accepted=Passage.npc_decide(c,e['wid'],e['first'],p['id'],'enter',at(c,e),new_source.event_id)
        assert accepted['status']=='npc_passed'
        assert Passage.player_traverse(c,e['wid'],e['player'],p['id'],at(c,e))['status']=='completed'


def test_bare_reply_and_rejected_or_expired_proposal_do_not_move(prepared):
    e=prepared
    with e['db'].write() as c:
        offset(c,e)
        p=propose(c,e)
        with pytest.raises(ValueError):
            Passage.npc_decide(c,e['wid'],e['first'],p['id'],'approach',at(c,e),p['proposal_event_id'])
        assert not c.execute('SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status=\'moving\'',(e['player'],e['first'])).fetchone()
        source=talk(c,e)
        assert Passage.npc_decide(c,e['wid'],e['first'],p['id'],'decline',at(c,e),source.event_id)['status']=='declined'
        with pytest.raises(ValueError,match='结束|过期'):
            Passage.player_approach(c,e['wid'],e['player'],p['id'],'invalid',at(c,e))
        assert not c.execute('SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status=\'moving\'',(e['player'],e['first'])).fetchone()


def test_approach_rejects_active_commitment_and_rolls_back(prepared):
    e=prepared
    with e['db'].write() as c:
        offset(c,e)
        p=propose(c,e)
        from world_engine.life import LifeActivityService
        actor=c.execute('SELECT * FROM characters WHERE id=?',(e['player'],)).fetchone()
        LifeActivityService.start(c,actor,at(c,e),'rest',10)
        count=c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
        with pytest.raises(ValueError):
            Passage.player_approach(c,e['wid'],e['player'],p['id'],'busy',at(c,e))
        assert not c.execute('SELECT 1 FROM character_movements WHERE character_id=? AND status=\'moving\'',(e['player'],)).fetchone()
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]==count


@pytest.mark.parametrize('fault',['missing_event','missing_knowledge','forged_wonder'])
def test_real_proposal_source_required_for_context_and_new_walk(prepared,fault):
    e=prepared
    with e['db'].write() as c:
        offset(c,e)
        p=propose(c,e)
        if fault=='missing_event':
            c.execute('DELETE FROM world_events WHERE id=?',(p['proposal_event_id'],))
        elif fault=='missing_knowledge':
            c.execute('DELETE FROM character_event_knowledge WHERE event_id=? AND character_id=?',(p['proposal_event_id'],e['first']))
        else:
            c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?",(p['proposal_event_id'],))
        assert Passage.npc_context(c,e['wid'],e['first'],e['player'],at(c,e))==[]
        before=c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
        with pytest.raises(ValueError):
            Passage.player_approach(c,e['wid'],e['player'],p['id'],'no-source',at(c,e))
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]==before
        assert not c.execute('SELECT 1 FROM character_movements WHERE character_id=? AND status=\'moving\'',(e['player'],)).fetchone()


def test_player_pass_requires_real_previous_npc_response_source(prepared):
    e=prepared
    with e['db'].write() as c:
        p=propose(c,e)
        source=talk(c,e)
        assert Passage.npc_decide(c,e['wid'],e['first'],p['id'],'enter',at(c,e),source.event_id)['status']=='npc_passed'
        c.execute('UPDATE world_events SET target_id=NULL WHERE id=?',(source.event_id,))
        with pytest.raises(ValueError,match='原事件'):
            Passage.player_traverse(c,e['wid'],e['player'],p['id'],at(c,e))
        assert c.execute('SELECT space_layer FROM characters WHERE id=?',(e['player'],)).fetchone()[0]=='ground'


def test_unverified_wonder_is_not_an_option_or_usable_passage(prepared):
    e=prepared
    with e['db'].write() as c:
        c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?",(e['city']['completed_event_id'],))
        assert not Passage.options(c,e['wid'],e['player'],at(c,e))['wonders']
        with pytest.raises(ValueError,match='开放来源'):
            propose(c,e)
