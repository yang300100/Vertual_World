"""哥哥，共同跨层只通过真实通道，各人选择和移动分别留痕。"""

from uuid import uuid4
import json

import pytest

from test_wonders import wonder_world, build  # noqa: F401
from world_engine.actions import ActionService
from world_engine.cross_layer import CrossLayerPassageService as Passage
from world_engine.decisions import NpcReply
from world_engine.domain import ActionProposal, ActionType
from world_engine.npc_performance import settle_npc_reply
from world_engine.repository import from_iso


@pytest.fixture
def prepared(wonder_world):
    e=wonder_world
    city=build(e)
    e['city']=city
    with e['db'].write() as c:
        source=c.execute("SELECT event_type,occurred_at FROM world_events WHERE id=?",(city['completed_event_id'],)).fetchone()
        assert source
        # 哥哥，已有奇观测试夹具的造时心跳可能先生成未来竣工事件；本隔离夹具以事件发生时刻作为场景起点。
        if from_iso(source['occurred_at'])>at(c,e):
            c.execute('UPDATE worlds SET current_time=? WHERE id=?',(source['occurred_at'],e['wid']))
        check=c.execute("SELECT occurred_at FROM world_events WHERE id=? AND world_id=? AND event_type='world.wonder_completed'",(city['completed_event_id'],e['wid'])).fetchone()
        assert check and from_iso(check[0])<=at(c,e),(check,at(c,e),city['completed_event_id'],e['wid'])
        for cid in (e['player'],e['first']):
            # 哥哥，仅在独立夹具设置通道前初始位置，不改真实存档。
            c.execute("UPDATE characters SET longitude=?,latitude=?,space_layer='ground',current_location_id=?,location_id=?,current_room_id=NULL,current_fixture_id=NULL,active_vehicle_id=NULL WHERE id=?",
                      (city['longitude'],city['latitude'],city['ground_location_id'],city['ground_location_id'],cid))
    return e


def at(c,e):
    return from_iso(c.execute('SELECT w.current_time FROM worlds w WHERE w.id=?',(e['wid'],)).fetchone()[0])


def propose(c,e,key='one'):
    return Passage.propose(c,e['wid'],e['player'],e['first'],e['city']['id'],'enter',key,at(c,e))


def talk(c,e):
    return ActionService().execute(c,world_id=e['wid'],tick_id=str(uuid4()),occurred_at=at(c,e),
        proposal=ActionProposal(actor_id=e['player'],target_id=e['first'],action=ActionType.SOCIALIZE,
                                reason='隔离场景',dialogue='你愿意一起通过升降通道吗？'))


def test_both_individually_choose_and_use_real_passage(prepared):
    e=prepared
    with e['db'].write() as c:
        before=c.execute('SELECT version FROM worlds WHERE id=?',(e['wid'],)).fetchone()[0]
        proposal=propose(c,e)
        assert proposal['status']=='pending'
        assert c.execute('SELECT version FROM worlds WHERE id=?',(e['wid'],)).fetchone()[0]==before+1
        assert propose(c,e)['id']==proposal['id']
        assert c.execute('SELECT COUNT(*) FROM cross_layer_passages').fetchone()[0]==1
        assert Passage.npc_context(c,e['wid'],e['first'],e['player'],at(c,e))[0]['id']==proposal['id']
        outcome=talk(c,e)
        assert outcome.accepted
        a=dict(c.execute('SELECT rowid,* FROM world_events WHERE id=?',(proposal['proposal_event_id'],)).fetchone())
        b=dict(c.execute('SELECT rowid,* FROM world_events WHERE id=?',(outcome.event_id,)).fetchone())
        assert a['event_type']=='social.cross_layer_proposed' and b['event_type']=='action.socialize',(a,b)
        assert all(json.loads(a['payload_json']).get(k)==v for k,v in {'passage_id':proposal['id'],'wonder_id':e['city']['id'],'direction':'enter'}.items()),a
        assert (a['actor_id'],a['target_id'],b['actor_id'],b['target_id'])==(e['player'],e['first'],e['player'],e['first']),(a,b)
        assert b['rowid']>a['rowid'] and a['occurred_at']<=b['occurred_at'],(a,b)
        npc=c.execute('SELECT * FROM characters WHERE id=?',(e['first'],)).fetchone()
        reply=NpcReply.model_validate({'reply':'好，我自己先通过。','action_intent':{'kind':'cross_layer','target_id':proposal['id'],'operation':'enter'}})
        settled=settle_npc_reply(c,npc=npc,player_id=e['player'],at=at(c,e),channel='in_person',reply=reply,source_event_id=outcome.event_id)
        assert settled.action_status=='applied'
        assert c.execute('SELECT space_layer FROM characters WHERE id=?',(e['first'],)).fetchone()[0]==e['city']['space_layer']
        assert c.execute('SELECT space_layer FROM characters WHERE id=?',(e['player'],)).fetchone()[0]=='ground'
        repeat=Passage.npc_decide(c,e['wid'],e['first'],proposal['id'],'enter',at(c,e),outcome.event_id)
        assert repeat['status']=='npc_passed' and repeat['npc_event_id']==settled.action_event_id
        result=Passage.player_traverse(c,e['wid'],e['player'],proposal['id'],at(c,e))
        assert result['status']=='completed' and result['player_event_id'] != result['npc_event_id']
        assert c.execute('SELECT space_layer FROM characters WHERE id=?',(e['player'],)).fetchone()[0]==e['city']['space_layer']
        assert Passage.player_traverse(c,e['wid'],e['player'],proposal['id'],at(c,e))['player_event_id']==result['player_event_id']


@pytest.mark.parametrize('operation',['decline','no_action'])
def test_refusal_or_unstructured_reply_never_moves_anyone(prepared,operation):
    e=prepared
    with e['db'].write() as c:
        p=propose(c,e); outcome=talk(c,e)
        npc=c.execute('SELECT * FROM characters WHERE id=?',(e['first'],)).fetchone()
        data={'reply':'我想再考虑。'}
        if operation=='decline': data['action_intent']={'kind':'cross_layer','target_id':p['id'],'operation':'decline'}
        settle_npc_reply(c,npc=npc,player_id=e['player'],at=at(c,e),channel='in_person',reply=NpcReply.model_validate(data),source_event_id=outcome.event_id)
        assert c.execute('SELECT space_layer FROM characters WHERE id=?',(e['first'],)).fetchone()[0]=='ground'
        assert c.execute('SELECT space_layer FROM characters WHERE id=?',(e['player'],)).fetchone()[0]=='ground'
        assert c.execute('SELECT status FROM cross_layer_passages WHERE id=?',(p['id'],)).fetchone()[0]==('declined' if operation=='decline' else 'pending')
        with pytest.raises(ValueError):Passage.player_traverse(c,e['wid'],e['player'],p['id'],at(c,e))


def test_wrong_source_remote_or_outside_node_cannot_move(prepared):
    e=prepared
    with e['db'].write() as c:
        p=propose(c,e)
        npc=c.execute('SELECT * FROM characters WHERE id=?',(e['first'],)).fetchone()
        action=NpcReply.model_validate({'reply':'好。','action_intent':{'kind':'cross_layer','target_id':p['id'],'operation':'enter'}})
        assert settle_npc_reply(c,npc=npc,player_id=e['player'],at=at(c,e),channel='letter',reply=action,source_event_id=p['proposal_event_id']).action_status=='rejected'
        with pytest.raises(ValueError):Passage.npc_decide(c,e['wid'],e['first'],p['id'],'enter',at(c,e),p['proposal_event_id'])
        outcome=talk(c,e)
        c.execute('UPDATE characters SET longitude=longitude+0.01 WHERE id=?',(e['first'],))
        with pytest.raises(ValueError):Passage.npc_decide(c,e['wid'],e['first'],p['id'],'enter',at(c,e),outcome.event_id)
        assert c.execute('SELECT status FROM cross_layer_passages WHERE id=?',(p['id'],)).fetchone()[0]=='pending'
