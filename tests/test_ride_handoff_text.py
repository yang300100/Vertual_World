"""哥哥，交接驾驶的文字与接口在独立存档核验，不把受控回应称为NPC意愿。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_ride_text_api import ride_world, rows, answer  # noqa: F401
from test_ride_recovery_text import recovery_world, now, physical, block  # noqa: F401
from world_engine.api_dialogue import NpcTextResult
from world_engine.api_intimacy import NaturalSocialChoice, compatible_choice_digests
from world_engine.contracts import ContractService
from world_engine.decisions import NpcReply
from world_engine.intent_parser import IntentParseResult
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText
from world_engine.repository import to_iso
from world_engine.rides import RideIntent

WORDS="我本人明确选择这份有限驾驶交接的完整条款。"


def current(e):
    return next(row for row in rows(e) if row['id']==e.ride)


@pytest.fixture
def handoff_world(recovery_world):
    e=recovery_world
    with e.db.write() as c:
        # 哥哥，原驾驶者仍能表达选择，但这份隔离故障条件下不能继续驾驶。
        c.execute('UPDATE characters SET energy=10 WHERE id=?',(e.player,))
    return e


def choice(e,operation,handoff_id=None,**extra):
    result={'operation':operation,'ride_id':e.ride,'statement':WORDS,**extra}
    if handoff_id is not None:result['handoff_id']=handoff_id
    return result


def submit(e,value,**extra):
    return e.client.post(f'/api/worlds/{e.wid}/player/social-choices',json={
        'request_id':str(uuid4()),'recipient_id':e.npc,'intent':WORDS,'evidence':WORDS,'ride_choice':value,**extra})


def proposal(e):
    with e.db.read() as c:deadline=to_iso(now(e,c)+timedelta(hours=1))
    return choice(e,'propose_handoff',controller_id=e.npc,deadline_world_time=deadline)


def propose(e):
    response=submit(e,proposal(e));assert response.status_code==200,response.text
    return current(e)['handoff']['id']


def npc_choice(e,cid,hid,operation='accept_handoff',*,plain=False,mixed=False):
    result={'reply':'这一轮交接我会按本人意愿独立决定。'}
    if not plain:result['ride_intent']=RideIntent(**choice(e,operation,hid))
    if mixed:result['companion_intent']={'operation':'propose','minutes':10,'statement':'同时提出另一同行。'}
    e.model.respond_to_player=lambda **kwargs:NpcReply(**result)
    response=e.client.post(f'/api/worlds/{e.wid}/player/act',json={
        'intent':'说：请独立决定这轮交接，不需要勉强。','target_character_id':cid})
    assert response.status_code==201,response.text
    return response


def authorize(e,hid):
    response=submit(e,choice(e,'authorize_handoff',hid));assert response.status_code==200,response.text
    return response


def accepted(e,hid):
    for cid in (e.npc,e.third):npc_choice(e,cid,hid)


def preview(e,value,*,evidence=WORDS,more=None):
    requests=[]
    def complete(*,schema,**request):
        requests.append(request)
        return SimpleNamespace(data=schema.validate_python({'operation':'ride','target_character_id':e.npc,
            'ride_choice':value,'choice_evidence':evidence,**(more or {})}))
    e.model.complete=complete
    response=e.client.post(f'/api/worlds/{e.wid}/player/intents/preview',json={'intent':WORDS,'target_character_id':e.npc})
    assert response.status_code==200,response.text
    return response.json(),requests


@pytest.mark.parametrize('operation',['propose_handoff','authorize_handoff','accept_handoff','decline_handoff','withdraw_handoff','take_control','revoke_handoff'])
def test_typed_handoff_operations_are_preserved_in_all_npc_channels(operation):
    value={'operation':operation,'ride_id':'original-ride','statement':WORDS}
    if operation=='propose_handoff':value.update(controller_id='new-driver',deadline_world_time='2030-01-01T12:00:00+00:00')
    else:value['handoff_id']='current-handoff'
    for schema,data in ((NpcReply,{'reply':'本人选择。'}),(NpcTextResult,{'reply':'本人选择。'}),
            (OutreachText,{'should_contact':True,'reply':'本人选择。'})):
        typed=schema.model_validate({**data,'ride_intent':value}).ride_intent
        assert typed.operation==operation and typed.ride_id=='original-ride'
    assert IntentParseResult.model_validate({'operation':'ride','ride_choice':value}).ride_choice.operation==operation


def test_player_semantic_preview_is_read_only_and_tells_model_the_new_protocol(handoff_world):
    e=handoff_world;before=physical(e,e.ride)
    parsed,requests=preview(e,proposal(e))
    assert parsed['operation']=='ride' and parsed['route_kind']=='social_choice'
    assert physical(e,e.ride)==before and current(e).get('handoff') is None
    assert requests and 'authorize_handoff' in requests[0]['system_prompt'] and 'take_control' in requests[0]['system_prompt']
    response=submit(e,parsed['ride_choice']);assert response.status_code==200,response.text
    row=current(e)
    assert row['status']=='blocked' and row['handoff']['status']=='pending'
    assert [m['decision'] for m in row['handoff']['members']]==['accepted','pending','pending']
    assert row['handoff']['authority']['grant_event_id'] is None
    assert physical(e,e.ride)['movement']==before['movement']
    assert physical(e,e.ride)['members']==before['members']


def test_permission_personal_acceptance_and_actual_take_control_are_separate(handoff_world):
    e=handoff_world;hid=propose(e);before=physical(e,e.ride)
    authorize(e,hid)
    assert [m['decision'] for m in current(e)['handoff']['members']]==['accepted','pending','pending']
    accepted(e,hid)
    ready=current(e)
    assert ready['handoff']['status']=='accepted' and ready['status']=='blocked'
    assert physical(e,e.ride)['movement']==before['movement']
    assert submit(e,choice(e,'take_control',hid)).status_code==409
    npc_choice(e,e.npc,hid,'take_control')
    after=physical(e,e.ride);row=current(e)
    assert row['status']=='moving' and row['effective_controller_id']==e.npc
    assert row['controller_id']==e.player
    assert after['movement']['id']==before['movement']['id']
    assert after['movement']['character_id']==e.player
    assert after['movement']['route_json']==before['movement']['route_json']
    assert after['movement']['distance_travelled_km']==before['movement']['distance_travelled_km']
    assert after['people']==before['people'] and after['members']==before['members']


def test_all_acceptance_without_legal_holder_authorization_cannot_move(handoff_world):
    e=handoff_world;hid=propose(e);accepted(e,hid);before=physical(e,e.ride)
    npc_choice(e,e.npc,hid,'take_control')
    assert current(e)['status']=='blocked' and physical(e,e.ride)==before
    assert current(e)['handoff']['authority']['grant_event_id'] is None


@pytest.mark.parametrize('mixed',[False,True])
def test_plain_words_or_mixed_effects_cannot_grant_new_driver_consent(handoff_world,mixed):
    e=handoff_world;hid=propose(e);authorize(e,hid);before=physical(e,e.ride)
    npc_choice(e,e.npc,hid,plain=not mixed,mixed=mixed)
    row=current(e)
    assert next(m for m in row['handoff']['members'] if m['character_id']==e.npc)['decision']=='pending'
    assert row['status']=='blocked' and physical(e,e.ride)['movement']==before['movement']


def test_passenger_cannot_authorize_or_revoke_the_real_holders_permission(handoff_world):
    e=handoff_world;hid=propose(e);before=physical(e,e.ride)
    npc_choice(e,e.third,hid,'authorize_handoff')
    assert current(e)['handoff']['authority']['grant_event_id'] is None
    authorize(e,hid);grant=current(e)['handoff']['authority']['grant_event_id']
    npc_choice(e,e.third,hid,'revoke_handoff')
    assert current(e)['handoff']['authority']['grant_event_id']==grant
    assert current(e)['handoff']['authority']['revoked_at'] is None
    assert physical(e,e.ride)['people']==before['people']


def test_holder_revoke_blocks_takeover_without_erasing_grant_or_personal_acceptance(handoff_world):
    e=handoff_world;hid=propose(e);authorize(e,hid);accepted(e,hid)
    original=current(e)['handoff'];before=physical(e,e.ride)
    response=submit(e,choice(e,'revoke_handoff',hid));assert response.status_code==200,response.text
    row=current(e)
    assert row['handoff']['authority']['grant_event_id']==original['authority']['grant_event_id']
    assert row['handoff']['authority']['revoke_event_id'] and row['status']=='blocked'
    assert row['handoff']['members']==original['members']
    npc_choice(e,e.npc,hid,'take_control')
    assert physical(e,e.ride)['movement']==before['movement'] and current(e)['status']=='blocked'


def test_duplicate_authorization_has_own_original_receipt_and_changes_no_old_payload(handoff_world):
    e=handoff_world;hid=propose(e);request_id=str(uuid4());value=choice(e,'authorize_handoff',hid)
    first=submit(e,value,request_id=request_id);assert first.status_code==200,first.text
    with e.db.read() as c:events=[tuple(r) for r in c.execute('SELECT id,payload_json FROM world_events ORDER BY rowid')]
    assert submit(e,value,request_id=request_id).json()==first.json()
    another=submit(e,value);assert another.status_code==200 and another.json()['event_id']==first.json()['event_id']
    with e.db.read() as c:assert events==[tuple(r) for r in c.execute('SELECT id,payload_json FROM world_events ORDER BY rowid')]
    assert submit(e,{**value,'handoff_id':str(uuid4())},request_id=request_id).status_code==409


@pytest.mark.parametrize('operation',['board','propose_recovery'])
def test_old_receipt_digest_without_new_null_handoff_id_is_still_compatible(operation):
    value={'operation':operation,'ride_id':'old-ride','statement':WORDS}
    if operation=='propose_recovery':value['deadline_world_time']='2030-01-01T12:00:00+00:00'
    payload=NaturalSocialChoice.model_validate({'request_id':'legacy-handoff-request','recipient_id':'npc','intent':WORDS,'evidence':WORDS,'ride_choice':value})
    legacy=payload.model_dump(mode='json');legacy['ride_choice'].pop('handoff_id')
    digest=hashlib.sha256(json.dumps(legacy,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    assert digest in compatible_choice_digests(payload)


@pytest.mark.parametrize('change',['unknown_handoff','old_epoch','another_ride','evidence','mixed'])
def test_semantic_preview_cannot_invent_or_replace_a_handoff_authorization(handoff_world,change):
    e=handoff_world;hid=propose(e);value=choice(e,'authorize_handoff',hid);evidence=WORDS;extra=None
    if change=='unknown_handoff':value['handoff_id']=str(uuid4())
    elif change=='old_epoch':
        assert submit(e,choice(e,'withdraw_handoff',hid)).status_code==200
        propose(e)
    elif change=='another_ride':value['ride_id']=str(uuid4())
    elif change=='evidence':evidence='不是本轮玩家原话。'
    else:extra={'companion_choice':{'operation':'propose','minutes':10,'statement':WORDS}}
    parsed,_=preview(e,value,evidence=evidence,more=extra)
    assert parsed['operation']=='none'


@pytest.mark.parametrize('boundary',['proposal_time','source_event','knowledge_time','missing_source'])
def test_unknown_or_future_handoff_cannot_enter_current_view(handoff_world,boundary):
    e=handoff_world;hid=propose(e)
    with e.db.write() as c:
        row=c.execute('SELECT * FROM vehicle_ride_handoffs WHERE id=?',(hid,)).fetchone()
        future=to_iso(now(e,c)+timedelta(minutes=1));source=row['proposal_event_id']
        if boundary=='proposal_time':c.execute('UPDATE vehicle_ride_handoffs SET proposed_at=? WHERE id=?',(future,hid))
        elif boundary=='source_event':c.execute('UPDATE world_events SET occurred_at=? WHERE id=?',(future,source))
        elif boundary=='knowledge_time':c.execute('UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=?',(future,source))
        else:c.execute('DELETE FROM character_event_knowledge WHERE event_id=?',(source,))
    assert current(e)['handoff'] is None
    parsed,_=preview(e,choice(e,'authorize_handoff',hid));assert parsed['operation']=='none'


@pytest.mark.parametrize('boundary',['granted_at','source_event','knowledge_time'])
def test_future_permission_is_not_current_authority_for_take_control(handoff_world,boundary):
    e=handoff_world;hid=propose(e);authorize(e,hid);accepted(e,hid)
    with e.db.write() as c:
        row=c.execute('SELECT * FROM vehicle_ride_handoffs WHERE id=?',(hid,)).fetchone();future=to_iso(now(e,c)+timedelta(minutes=1))
        if boundary=='granted_at':c.execute('UPDATE vehicle_ride_handoffs SET granted_at=? WHERE id=?',(future,hid))
        elif boundary=='source_event':c.execute('UPDATE world_events SET occurred_at=? WHERE id=?',(future,row['authorization_event_id']))
        else:c.execute('UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=?',(future,row['authorization_event_id']))
    before=physical(e,e.ride);npc_choice(e,e.npc,hid,'take_control')
    assert current(e)['status']=='blocked' and physical(e,e.ride)['movement']==before['movement']


@pytest.mark.parametrize('channel',['letter','outreach_letter'])
def test_remote_npc_cannot_take_control_of_an_accepted_handoff(handoff_world,channel):
    e=handoff_world;hid=propose(e);authorize(e,hid);accepted(e,hid);before=physical(e,e.ride)
    with e.db.write() as c:
        npc=c.execute('SELECT * FROM characters WHERE id=?',(e.npc,)).fetchone()
        result=settle_npc_reply(c,npc=npc,player_id=e.player,at=now(e,c),channel=channel,
            reply=NpcReply(reply='我现在选择接管驾驶。',ride_intent=RideIntent(**choice(e,'take_control',hid))))
    assert result.action_status=='rejected' and physical(e,e.ride)==before


def test_finite_handoff_permission_never_becomes_general_vehicle_or_ownership_right(handoff_world):
    e=handoff_world;hid=propose(e);authorize(e,hid);accepted(e,hid);npc_choice(e,e.npc,hid,'take_control')
    with e.db.read() as c:
        assert ContractService.vehicle_for(c,e.wid,e.npc,e.vehicle,at=now(e,c)) is None
        assert c.execute('SELECT owner_character_id FROM vehicles WHERE id=?',(e.vehicle,)).fetchone()[0]==e.player
    response=e.client.post(f'/api/worlds/{e.wid}/player/social-choices',json={
        'request_id':str(uuid4()),'recipient_id':e.npc,'intent':WORDS,'evidence':WORDS,
        'ride_choice':{'operation':'propose','vehicle_id':e.vehicle,'member_ids':e.ids,'controller_id':e.npc,
            'destination_location_id':e.route[1],'deadline_world_time':to_iso(e.at+timedelta(hours=2)),'statement':WORDS}})
    assert response.status_code==409


def test_holder_revoke_after_take_control_suspends_without_reverting_actual_driver(handoff_world):
    e=handoff_world;hid=propose(e);authorize(e,hid);accepted(e,hid);npc_choice(e,e.npc,hid,'take_control')
    e.clock.heartbeat(e.wid,elapsed_seconds=10);before=physical(e,e.ride)
    response=submit(e,choice(e,'revoke_handoff',hid));assert response.status_code==200,response.text
    row=current(e)
    assert row['status']=='blocked' and row['effective_controller_id']==e.npc
    assert physical(e,e.ride)['people']==before['people']
    e.clock.heartbeat(e.wid,elapsed_seconds=10)
    assert physical(e,e.ride)['movement']['distance_travelled_km']==before['movement']['distance_travelled_km']
