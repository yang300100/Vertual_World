"""哥哥，通道协议只在可执行现场出现，结构化选择不能代替双方授权。"""

import copy
import json

import pytest
from pydantic import ValidationError

from test_ride_handoff_prompt import context_with_handoff
from test_cross_layer import prepared, at, talk  # noqa: F401
from test_cross_layer_approach import offset, propose
from test_wonders import wonder_world  # noqa: F401
from world_engine.decisions import NpcActionIntent, NpcReply
from world_engine.npc_performance import settle_npc_reply
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import NpcPromptOverflow, build_npc_prompt


def test_approach_protocol_is_conditional_and_preserves_current_facts():
    context = context_with_handoff()
    context['character_influences'] = [{'id': 'npc-boundary', 'content': '由我本人决定是否同行。'}]
    context['intimacy']['own_perspective'] = [{'concern': '希望对方尊重我的选择。'}]
    context['scene'] = {'cross_layer_passages': [{'id': 'actual-proposal', 'wonder_name': '公共通道',
        'direction': 'enter', 'npc_at_node': False, 'player_at_node': False, 'npc_can_approach': True}]}
    original = copy.deepcopy(context)
    prompt = build_npc_prompt(npc_reply_system_prompt(), context)
    packed = json.loads(prompt.messages[1]['content'])
    assert '公共通道' in prompt.messages[0]['content'] and 'approach' in prompt.messages[0]['content']
    assert packed['scene']['cross_layer_passages'] == context['scene']['cross_layer_passages']
    assert packed['intimacy']['rides'] == context['intimacy']['rides']
    assert packed['intimacy']['own_perspective'] == context['intimacy']['own_perspective']
    assert packed['character_influences'] == context['character_influences']
    assert prompt.input_tokens <= 8192 and context == original
    del context['scene']['cross_layer_passages']
    assert 'npc_can_approach时' not in build_npc_prompt(npc_reply_system_prompt(), context).messages[0]['content']
    context['intimacy']['rides'][0]['handoff']['terms']['statement'] = '本人已有条款必须完整保留。' * 2500
    with pytest.raises(NpcPromptOverflow):
        build_npc_prompt(npc_reply_system_prompt(), context)


def test_approach_operation_only_valid_for_cross_layer():
    assert NpcActionIntent.model_validate({'kind': 'cross_layer', 'target_id': 'p', 'operation': 'approach'}).operation == 'approach'
    with pytest.raises(ValidationError):
        NpcActionIntent.model_validate({'kind': 'door', 'target_id': 'd', 'operation': 'approach'})


@pytest.mark.parametrize('channel,mixed', [('letter', False), ('in_person', True)])
def test_remote_or_mixed_approach_cannot_move_or_create_other_state(prepared, channel, mixed):
    e = prepared
    with e['db'].write() as c:
        offset(c, e)
        proposal = propose(c, e)
        source = talk(c, e)
        npc = c.execute('SELECT * FROM characters WHERE id=?', (e['first'],)).fetchone()
        reply = {'reply': '我再考虑一下。', 'action_intent': {'kind': 'cross_layer',
                 'target_id': proposal['id'], 'operation': 'approach'}}
        if mixed:
            reply['companion_intent'] = {'operation': 'propose', 'minutes': 10, 'statement': '一起走。'}
        before_events = c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0]
        result = settle_npc_reply(c, npc=npc, player_id=e['player'], at=at(c,e), channel=channel,
                                  reply=NpcReply.model_validate(reply), source_event_id=source.event_id)
        assert result.action_status == 'rejected'
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e['first'],)).fetchone()
        assert c.execute('SELECT status FROM cross_layer_passages WHERE id=?', (proposal['id'],)).fetchone()[0] == 'pending'
        assert c.execute('SELECT COUNT(*) FROM world_events').fetchone()[0] == before_events
