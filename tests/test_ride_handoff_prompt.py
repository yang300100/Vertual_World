"""哥哥，交接条款、合法授权与本人偏好完整进入8192硬门禁。"""

# ruff: noqa: F811
import copy
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from pydantic import TypeAdapter
from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401
from test_ride_text_api import ride_world  # noqa: F401
from test_ride_recovery_text import recovery_world,block,now,choose as recovery_choice,current as recovery_current  # noqa: F401
from test_ride_recovery_text import propose as propose_recovery,npc_choice as recovery_npc_choice,submit as recovery_submit
from test_ride_handoff_text import handoff_world,propose,authorize,accepted,npc_choice  # noqa: F401
from test_ride_prompt_budget import current_ride
from world_engine.agent_llm import AgentModelBackend
from world_engine.engine import WorldEngine
from world_engine.outreach import OutreachText
from world_engine.roleplay import npc_reply_system_prompt,scoped_npc_protocols
from world_engine.roleplay_pipeline import NpcPromptOverflow,build_npc_prompt,estimate_chat_tokens
from world_engine.repository import to_iso


def handoff(**changes):
    return {'id':'handoff-2','ride_id':'current-ride','epoch':2,'status':'pending',
        'terms':{'handoff_id':'handoff-2','ride_id':'current-ride','epoch':2,
            'old_controller_id':'player','controller_id':'npc','movement_id':'original-movement','block_event_id':'actual-block',
            'vehicle_id':'original-vehicle','destination_location_id':'original-garden','deadline_at':'2030-01-01T13:00:00+00:00',
            'member_ids':['player','npc','third'],'remaining_seconds':600,'remaining_distance_km':2,
            'route_sha256':'original-route-hash','snapshot_sha256':'frozen-complete-occupancy-snapshot',
            'authority_source':{'holder_id':'player','source_kind':'owner','source_id':'original-vehicle'},
            'stop_requested':True,'stop_policy':'只按原路线抵达原安全目的地后由本人下车。',
            'statement':'原车主可以授权，所有在车者仍各自同意，指定的新驾驶者另行接管。'},
        'members':[{'character_id':cid,'decision':'accepted' if cid=='player' else 'pending',
            'decision_at':'2030-01-01T11:00:00+00:00' if cid=='player' else None,
            'decision_event_id':'handoff-proposed' if cid=='player' else None} for cid in ('player','npc','third')],
        'authority':{'holder_id':'player','source_kind':'owner','source_id':'original-vehicle','granted_at':None,
            'grant_event_id':None,'revoked_at':None,'revoke_event_id':None},
        'source_event_id':'handoff-proposed','can_accept':True,'can_decline':True,'can_withdraw':False,
        'can_authorize':False,'can_take_control':False,'can_revoke':False,**changes}


def context_with_handoff(plan=None):
    return {'player_text':'这轮交接请按本人的意愿选择。','intimacy':{'rides':[current_ride(status='blocked',
        handoff=plan or handoff(),effective_controller_id='player',can_propose_handoff=False)]}}


def test_actual_handoff_context_keeps_original_terms_complete_new_terms_and_personal_authority(handoff_world):
    e=handoff_world;hid=propose(e);engine=WorldEngine(e.db,e.settings,e.model)
    try:
        with e.db.read() as c:
            snap=engine.repository.get_snapshot(c,e.wid)
            context=engine.dialogue_context.build(c,snapshot=snap,npc=snap.character_by_id(e.npc),player=snap.character_by_id(e.player),
                player_text='原控制者不能继续驾驶，请只按你本人意愿决定是否接手，不必勉强。',conversation=None,channel='in_person',
                interaction='隔离在车成员对真实待回应交接条款分别作选择。')
        before=copy.deepcopy(context);packed=build_npc_prompt(npc_reply_system_prompt(),context)
        value=json.loads(packed.messages[1]['content'])
        assert value['intimacy']['rides']==context['intimacy']['rides']
        current=value['intimacy']['rides'][0]
        assert current['terms']['controller_id']==e.player
        assert current['handoff']['terms']['handoff_id']==hid
        assert current['handoff']['terms']['controller_id']==e.npc
        assert current['handoff']['terms']['member_ids']==e.ids
        assert current['status']=='blocked'
        assert 'authorize_handoff' in packed.messages[0]['content'] and 'take_control' in packed.messages[0]['content']
        assert estimate_chat_tokens(packed.messages)<=8192 and context==before
    finally:engine.close()


def test_private_preference_and_current_handoff_cannot_be_trimmed_for_old_history():
    context=context_with_handoff()
    preference={'id':'registered-driver-boundary','content':'我可以拒绝驾驶，旧的乘车同意并没有赋予任何人替我接管的权限。',
        'provenance':{'knowledge_level':'character_belief','author_character_id':'npc','source_event_id':'registered-boundary-source'}}
    context['character_influences']=[preference]
    context['intimacy']['own_perspective']=[{'concern':'我希望停靠请求仍被尊重，并且实际授权清楚后再作决定。'}]
    context['recent_private_memories']=[{'summary':'无关的旧闲谈。'*10000}]
    before=copy.deepcopy(context);prompt=build_npc_prompt(npc_reply_system_prompt(),context)
    packed=json.loads(prompt.messages[1]['content'])
    assert packed['intimacy']['rides']==context['intimacy']['rides']
    assert packed['character_influences']==[preference]
    assert packed['intimacy']['own_perspective']==context['intimacy']['own_perspective']
    assert prompt.input_tokens<=8192 and context==before


def test_required_handoff_overflow_is_rejected_without_omitting_the_holder_or_members():
    plan=handoff();plan['terms']['statement']='本轮完整的驾驶交接条款不可裁剪。'*2500
    context=context_with_handoff(plan);original=copy.deepcopy(context)
    with pytest.raises(NpcPromptOverflow):build_npc_prompt(npc_reply_system_prompt(),context)
    assert context==original


def test_normal_ride_without_handoff_does_not_receive_an_unusable_control_protocol():
    context={'intimacy':{'rides':[current_ride(status='moving',handoff=None,can_propose_handoff=False)]}}
    system,_=scoped_npc_protocols(npc_reply_system_prompt(),context)
    assert 'authorize_handoff' not in system and 'take_control' not in system


@pytest.mark.parametrize('key',['journeys','overnights','separate_stays','lodging_plans'])
def test_other_real_current_commitments_keep_their_protocols_during_handoff(key):
    context=context_with_handoff();context['intimacy'][key]=[{'id':'other-real-arrangement','status':'paused'}]
    system,_=scoped_npc_protocols(npc_reply_system_prompt(),context)
    assert 'authorize_handoff' in system and 'intimacy.overnights是旅途长时住宿' in system
    assert '访问许可仅在本次时间窗' in system


@pytest.mark.parametrize('key,start,end',[
    ('shared_moments','可以邀请或回应共同生活，使用shared_life_intent：','intimacy.appointments是未来共同安排'),
    ('shared_plans','本人明确提出具体分工须同步shared_plan_intent.propose','intimacy.family是婚姻'),
    ('companion_proposals','短时同行由本人独立用companion_intent选择：','订房用lodging_plan_intent，'),
])
def test_existing_current_record_keeps_its_complete_protocol_during_handoff(key,start,end):
    context=context_with_handoff()
    context['intimacy'][key]=[{'id':'current-record-with-source','status':'pending','source_event_id':'known-current-source',
        'statement':'这份现行约定仍须由本人按原条款回应，不因驾驶交接而抹去。'}]
    original=copy.deepcopy(context);full=npc_reply_system_prompt()
    protocol=full[full.index(start):full.index(end,full.index(start))]
    system,_=scoped_npc_protocols(full,context)
    assert protocol in system and context==original
    prompt=build_npc_prompt(full,context);packed=json.loads(prompt.messages[1]['content'])
    assert protocol in prompt.messages[0]['content']
    assert packed['intimacy'][key]==context['intimacy'][key]
    assert packed['intimacy']['rides']==context['intimacy']['rides']
    assert prompt.input_tokens<=8192 and context==original


def test_empty_current_records_omit_unusable_detail_but_keep_body_autonomy_and_complete_facts():
    context=context_with_handoff()
    context['companionship']=[]
    context['npc_card']={'name':'独立选择的驾驶者','personality':'温和而谨慎','background':'珍惜已经表达过的边界。'}
    context['character_influences']=[{'id':'independent-current-boundary','content':'是否驾驶与是否身体接触由我分别决定。','source_event_id':'known-personal-source'}]
    context['intimacy'].update(shared_moments=[],shared_plans=[],companion_proposals=[],
        expressed_boundaries=[{'topic':'身体接触','statement':'请先取得我本次明确同意。'}],
        own_perspective=[{'concern':'我想独立选择驾驶，并继续尊重每次身体接触的边界。'}],
        ongoing_considerations=[{'meaning':'交接没有使旧顾虑消失。','source_event_id':'known-concern-source'}])
    before=copy.deepcopy(context);prompt=build_npc_prompt(npc_reply_system_prompt(),context)
    system=prompt.messages[0]['content'];packed=json.loads(prompt.messages[1]['content'])
    assert '可以邀请或回应共同生活，使用shared_life_intent：' not in system
    assert '短时同行由本人独立用companion_intent选择：' not in system
    assert '本人明确提出具体分工须同步shared_plan_intent.propose' not in system
    assert '共同活动及身体接触仍须各自同意' in system
    assert all(operation in system for operation in ('authorize_handoff','accept_handoff','take_control'))
    assert packed['npc_card']==context['npc_card'] and packed['character_influences']==context['character_influences']
    for key in ('rides','expressed_boundaries','own_perspective','ongoing_considerations'):
        assert packed['intimacy'][key]==context['intimacy'][key]
    assert prompt.input_tokens<=8192 and context==before


def test_mock_transport_preserves_typed_handoff_without_extra_or_external_model_requests(settings):
    requests=[];raw={'should_contact':True,'reply':'这份交接我本人暂时不接受。','ride_intent':{
        'operation':'decline_handoff','ride_id':'current-ride','handoff_id':'handoff-2','statement':'我本人婉拒本轮交接。'}}
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(raw,ensure_ascii=False)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler),base_url='http://test/') as client:
        backend=AgentModelBackend(replace(settings,deepseek_api_key='test',deepseek_max_retries=0),client)
        result=backend.complete(label='npc_outreach',system_prompt=npc_reply_system_prompt(),user_payload=context_with_handoff(),
            schema=TypeAdapter(OutreachText),roleplay=True)
    assert len(requests)==1 and result.data.ride_intent.operation=='decline_handoff'
    assert result.data.ride_intent.handoff_id=='handoff-2'
    assert estimate_chat_tokens(requests[0]['messages'])<=8192


@pytest.mark.parametrize('scenario',['recovery_then_handoff','handoff_then_recovery'])
def test_old_executed_control_source_and_new_real_choice_fit_without_losing_facts(recovery_world,scenario):
    e=recovery_world
    if scenario=='recovery_then_handoff':
        rid=propose_recovery(e)
        for cid in (e.npc,e.third):recovery_npc_choice(e,cid,e.ride,rid)
        response=recovery_submit(e,recovery_choice(e,e.ride,'resume',rid))
        assert response.status_code==200,response.text
        e.clock.heartbeat(e.wid,elapsed_seconds=5);block(e,e.ride,half=False)
        with e.db.write() as c:c.execute('UPDATE characters SET energy=10 WHERE id=?',(e.player,))
        propose(e)
    else:
        with e.db.write() as c:c.execute('UPDATE characters SET energy=10 WHERE id=?',(e.player,))
        hid=propose(e);authorize(e,hid);accepted(e,hid);npc_choice(e,e.npc,hid,'take_control')
        e.clock.heartbeat(e.wid,elapsed_seconds=5);block(e,e.ride,half=False)
        with e.db.read() as c:deadline=to_iso(now(e,c)+timedelta(minutes=30))
        response=recovery_submit(e,recovery_choice(e,e.ride,'propose_recovery',deadline_world_time=deadline))
        assert response.status_code==200,response.text
    view=recovery_current(e,e.ride)
    assert view['recovery']['status']==('executed' if scenario=='recovery_then_handoff' else 'pending')
    assert view['handoff']['status']==('pending' if scenario=='recovery_then_handoff' else 'executed')
    engine=WorldEngine(e.db,e.settings,e.model)
    try:
        with e.db.read() as c:
            snap=engine.repository.get_snapshot(c,e.wid)
            context=engine.dialogue_context.build(c,snapshot=snap,npc=snap.character_by_id(e.npc),player=snap.character_by_id(e.player),
                player_text='这轮的新选择请按本人意愿决定，旧驾驶控制来源和全部当前承诺都仍须尊重。',
                conversation=None,channel='in_person',interaction='隔离组合：已执行的旧控制来源、再次真实挂起与待回应新条款。')
        before=copy.deepcopy(context);prompt=build_npc_prompt(npc_reply_system_prompt(),context)
        packed=json.loads(prompt.messages[1]['content'])
        assert prompt.input_tokens<=8192 and context==before
        assert packed['intimacy']['rides']==context['intimacy']['rides']
        assert packed['intimacy']['own_perspective']==context['intimacy']['own_perspective']
        assert packed.get('character_influences')==context.get('character_influences')
    finally:engine.close()
