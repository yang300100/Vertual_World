"""哥哥，全部样本都在隔离世界验证，不把模拟回应当作真实意愿。"""

import copy
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from test_rides import ride_world, current  # noqa: F401
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_ride_recovery import blocked
from test_ride_recovery_text import recovery_world as original_recovery_world, ride_world as text_ride_world  # noqa: F401
from test_ride_handoff_prompt import context_with_handoff
from world_engine.decisions import NpcReply
from world_engine.actions import ActionService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.npc_performance import settle_npc_reply
from world_engine.npc_stage_actions import self_care_options, self_rest_available
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import build_npc_prompt, NpcPromptOverflow


def source(c, e, rid):
    return c.execute("SELECT board_event_id FROM vehicle_ride_members WHERE ride_id=? AND character_id=?", (rid, e.npc)).fetchone()[0]


@pytest.fixture
def selfcare_text_world(text_ride_world, request):
    return original_recovery_world.__wrapped__(text_ride_world, request)


def settle(c, e, rid, reply, channel="in_person", event=None):
    npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
    return settle_npc_reply(c, npc=npc, player_id=e.player, at=current(e,c), channel=channel,
                            reply=NpcReply.model_validate(reply), source_event_id=event or source(c,e,rid))


def add_medicine(c, e):
    iid = str(uuid4())
    c.execute("INSERT INTO item_types(id,name,category,stack_limit,usable,heal) VALUES (?,'隔离药','consumable',10,1,10)", (iid,))
    c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,owner_character_id,quantity,condition) VALUES (?,?,?,?,'character_inventory',?,2,100)",
              (iid,e.wid,iid,e.npc,e.npc))
    c.execute("UPDATE characters SET health=30 WHERE id=?", (e.npc,))
    return iid


def test_personal_medicine_receipt_is_idempotent_and_located_in_vehicle(ride_world):
    e=ride_world; rid=blocked(e)
    with e.db.write() as c:
        iid=add_medicine(c,e); src=source(c,e,rid)
        assert iid in [i['id'] for i in self_care_options(c, c.execute('SELECT * FROM characters WHERE id=?',(e.npc,)).fetchone(),current(e,c))]
        choice={'reply':'我决定先服用自己的药。','action_intent':{'kind':'self_care','target_id':iid}}
        first=settle(c,e,rid,choice,event=src); assert first.action_status=='applied'
        second=settle(c,e,rid,choice,event=src); assert second.action_status=='already'
        assert c.execute('SELECT health FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==40
        assert c.execute('SELECT quantity FROM item_instances WHERE id=?',(iid,)).fetchone()[0]==1
        ev=c.execute('SELECT * FROM world_events WHERE id=?',(first.action_event_id,)).fetchone()
        assert ev['location_id'] is None
        assert json.loads(ev['payload_json'])['aboard_selfcare']=={'ride_id':rid,'vehicle_id':e.vehicle}
        assert settle(c,e,rid,{'reply':'我想休息。','action_intent':{'kind':'self_rest','target_id':e.npc,'minutes':10}},event=src).action_status=='rejected'


@pytest.mark.parametrize('channel,mixed', [('letter',False),('in_person',True)])
def test_remote_or_mixed_choice_has_no_effect(ride_world,channel,mixed):
    e=ride_world;rid=blocked(e)
    with e.db.write() as c:
        iid=add_medicine(c,e)
        reply={'reply':'我先照顾自己。','action_intent':{'kind':'self_care','target_id':iid}}
        if mixed:reply['companion_intent']={'operation':'propose','minutes':10,'statement':'一起走。'}
        result=settle(c,e,rid,reply,channel)
        assert result.action_status=='rejected'
        assert c.execute('SELECT health FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==30
        assert c.execute('SELECT quantity FROM item_instances WHERE id=?',(iid,)).fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM npc_selfcare_receipts').fetchone()[0]==0


def test_personal_rest_rechecks_source_and_does_not_control_companion(ride_world):
    e=ride_world;rid=blocked(e)
    with e.db.write() as c:
        npc=c.execute('SELECT * FROM characters WHERE id=?',(e.npc,)).fetchone()
        assert self_rest_available(c,npc)
        choice={'reply':'我决定休息十分钟。','action_intent':{'kind':'self_rest','target_id':e.npc,'minutes':10}}
        assert settle(c,e,rid,choice,event='unknown-event').action_status=='rejected'
        result=settle(c,e,rid,choice);assert result.action_status=='applied'
        assert c.execute("SELECT COUNT(*) FROM character_life_activities WHERE character_id=? AND status='running'",(e.npc,)).fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM character_life_activities WHERE character_id=? AND status='running'",(e.player,)).fetchone()[0]==0
        assert settle(c,e,rid,choice).action_status=='already'


@pytest.mark.parametrize('spoiled', [False, True])
def test_food_uses_actual_batch_and_location_without_spoiled_consumption(ride_world,spoiled):
    e=ride_world;rid=blocked(e)
    with e.db.write() as c:
        iid=str(uuid4())
        c.execute("INSERT INTO item_types(id,name,category,stack_limit) VALUES (?,'隔离食物','food',10)",(iid,))
        expires=current(e,c)+timedelta(minutes=-1 if spoiled else 60)
        c.execute("INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type,owner_character_id,quantity,condition,spoils_world_time) VALUES (?,?,?,?,'character_inventory',?,2,100,?)",
                  (iid,e.wid,iid,e.npc,e.npc,expires.isoformat()))
        c.execute("INSERT INTO material_modes(world_id) VALUES (?)",(e.wid,))
        MaterialService.attach(c,iid,MaterialState(mass=2,nutrition=40))
        c.execute('UPDATE characters SET satiety=30 WHERE id=?',(e.npc,))
        result=settle(c,e,rid,{'reply':'我决定吃自己的食物。','action_intent':{'kind':'self_care','target_id':iid}})
        assert result.action_status==('rejected' if spoiled else 'applied')
        assert c.execute('SELECT quantity FROM item_instances WHERE id=?',(iid,)).fetchone()[0]==(2 if spoiled else 1)
        assert c.execute('SELECT satiety FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==(30 if spoiled else 50)
        if not spoiled:
            ev=c.execute('SELECT * FROM world_events WHERE id=?',(result.action_event_id,)).fetchone()
            assert ev['location_id'] is None
            assert json.loads(ev['payload_json'])['aboard_selfcare']=={'ride_id':rid,'vehicle_id':e.vehicle}
            assert c.execute('SELECT destination FROM material_sinks WHERE world_id=?',(e.wid,)).fetchone()[0]=='body:'+e.npc


def test_removed_source_preserves_receipt_and_prevents_recreated_source_replay(ride_world):
    e=ride_world;rid=blocked(e)
    with e.db.write() as c:
        iid=add_medicine(c,e)
        src=ActionService._record_event(c,world_id=e.wid,tick_id=str(uuid4()),occurred_at=current(e,c),event_type='social.selfcare_source',
            actor_id=e.npc,target_id=None,location_id=None,summary='本人参与的隔离事件。',payload={})
        original=dict(c.execute('SELECT * FROM world_events WHERE id=?',(src,)).fetchone())
        choice={'reply':'我决定服药。','action_intent':{'kind':'self_care','target_id':iid}}
        assert settle(c,e,rid,choice,event=src).action_status=='applied'
        c.execute('DELETE FROM world_events WHERE id=?',(src,))
        assert c.execute('SELECT COUNT(*) FROM npc_selfcare_receipts WHERE source_event_id=?',(src,)).fetchone()[0]==1
        assert settle(c,e,rid,choice,event=src).action_status=='rejected'
        keys=list(original); c.execute('INSERT INTO world_events('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',tuple(original[k] for k in keys))
        assert settle(c,e,rid,choice,event=src).action_status=='already'
        assert c.execute('SELECT quantity FROM item_instances WHERE id=?',(iid,)).fetchone()[0]==1


@pytest.mark.parametrize('fault',['location','other_movement'])
def test_medicine_requires_coherent_location_and_no_other_vehicle_movement(ride_world,fault):
    e=ride_world;rid=blocked(e)
    with e.db.write() as c:
        iid=add_medicine(c,e)
        if fault=='location':
            c.execute('UPDATE characters SET current_location_id=? WHERE id=?',(e.origin,e.npc))
        else:
            row=dict(c.execute('SELECT * FROM character_movements WHERE vehicle_id=?',(e.vehicle,)).fetchone())
            row.update(id=str(uuid4()),character_id=e.other,status='moving')
            keys=list(row);c.execute('INSERT INTO character_movements('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',tuple(row[k] for k in keys))
        npc=c.execute('SELECT * FROM characters WHERE id=?',(e.npc,)).fetchone()
        assert not self_rest_available(c,npc) and not self_care_options(c,npc,current(e,c))
        result=settle(c,e,rid,{'reply':'我打算用药。','action_intent':{'kind':'self_care','target_id':iid}})
        assert result.action_status=='rejected'
        assert c.execute('SELECT health FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==30
        assert c.execute('SELECT quantity FROM item_instances WHERE id=?',(iid,)).fetchone()[0]==2


@pytest.mark.parametrize('intent',[None,{'kind':'self_care','target_id':'虚构物品'}])
def test_unsettled_selfcare_cannot_be_presented_as_an_effect(ride_world,intent):
    e=ride_world;rid=blocked(e)
    with e.db.write() as c:
        reply={'reply':'我已经服下药了。','stage_direction':'她服药后睡着了。'}
        if intent:reply['action_intent']=intent
        result=settle(c,e,rid,reply)
        assert '服下' not in result.reply and '服药' not in (result.stage_direction or '')
        assert '睡着' not in (result.stage_direction or '')
        assert result.action_status==('rejected' if intent else None)


def test_local_player_dialogue_exposes_personal_options_and_settles_choice(selfcare_text_world):
    e=selfcare_text_world
    with e.db.write() as c:
        iid=add_medicine(c,e)
    seen=[]
    def respond(**kwargs):
        seen.append(kwargs['context'])
        return NpcReply.model_validate({'reply':'我决定用自己的药。',
            'action_intent':{'kind':'self_care','target_id':iid}})
    e.model.respond_to_player=respond
    response=e.client.post(f'/api/worlds/{e.wid}/player/act',json={
        'intent':'说：你可以按自己的情况照顾自己。','target_character_id':e.npc})
    assert response.status_code==201,response.text
    assert seen and iid in [item['id'] for item in seen[0]['scene']['aboard_selfcare']['items']]
    with e.db.read() as c:
        assert c.execute('SELECT health FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==40
        receipt=c.execute('SELECT * FROM npc_selfcare_receipts WHERE npc_id=?',(e.npc,)).fetchone()
        assert receipt and receipt['target_id']==iid
        assert c.execute('SELECT event_type FROM world_events WHERE id=?',(receipt['source_event_id'],)).fetchone()


def test_protocol_only_when_available_and_hard_budget_retains_original_facts():
    context=context_with_handoff();context['scene']={'aboard_selfcare':{'rest':True,'items':[{'id':'medicine','name':'药','kind':'medicine'}]}}
    context['character_influences']=[{'id':'own-boundary','content':'我自己决定照护。'}]
    context['intimacy']['own_perspective']=[{'concern':'我担心受伤。'}]
    original=copy.deepcopy(context);prompt=build_npc_prompt(npc_reply_system_prompt(),context)
    data=json.loads(prompt.messages[1]['content'])
    assert '车内本人照护' in prompt.messages[0]['content'] and 'self_care' in prompt.messages[0]['content']
    assert data['scene']['aboard_selfcare']==context['scene']['aboard_selfcare']
    assert data['intimacy']['rides']==context['intimacy']['rides']
    assert data['character_influences']==context['character_influences'] and data['intimacy']['own_perspective']==context['intimacy']['own_perspective']
    assert prompt.input_tokens<=8192 and context==original
    del context['scene']['aboard_selfcare']
    assert '车内本人照护' not in build_npc_prompt(npc_reply_system_prompt(),context).messages[0]['content']
    context['intimacy']['rides'][0]['handoff']['terms']['statement']='完整条款不可裁剪。'*2500
    with pytest.raises(NpcPromptOverflow):build_npc_prompt(npc_reply_system_prompt(),context)
