"""哥哥，真实 HTTP 与受控 NPC 回应串联实际通道，不冒称模型意愿。"""

from fastapi.testclient import TestClient

from test_cross_layer import prepared, at  # noqa: F401
from test_cross_layer_approach import offset, arrive
from test_wonders import wonder_world  # noqa: F401
from test_lived_world import LivingModel
from world_engine.api import create_app
from world_engine.decisions import NpcReply


def test_http_conversation_separately_walks_and_traverses(prepared, monkeypatch):
    e = prepared
    model = LivingModel()
    monkeypatch.setattr('world_engine.engine.build_decision_provider', lambda _: model)
    monkeypatch.setattr('world_engine.engine.build_agent_model_backend', lambda _: model)
    contexts = []
    with TestClient(create_app(e['settings'])) as client:
        with e['db'].write() as c:
            offset(c,e)
        base = f"/api/worlds/{e['wid']}"
        choice = {'npc_id': e['first'], 'wonder_id': e['city']['id'], 'direction': 'enter',
                  'idempotency_key': 'http-cross-layer-cycle'}
        proposal = client.post(base + '/cross-layer/proposals', json=choice)
        assert proposal.status_code == 200, proposal.text
        pid = proposal.json()['id']
        assert proposal.json()['status'] == 'pending'

        def respond_approach(**kwargs):
            contexts.append(kwargs['context'])
            return NpcReply.model_validate({'reply': '我自己先走近看看。', 'action_intent':
                {'kind': 'cross_layer', 'target_id': pid, 'operation': 'approach'}})
        model.respond_to_player = respond_approach
        first = client.post(base + '/player/act', json={'intent': '说：你愿意自己先走近通道吗？', 'target_character_id': e['first']})
        assert first.status_code == 201, first.text
        assert contexts and contexts[-1]['scene']['cross_layer_passages'][0]['npc_can_approach']
        with e['db'].read() as c:
            npc_move = c.execute("SELECT id FROM character_movements WHERE world_id=? AND character_id=? AND status='moving'", (e['wid'],e['first'])).fetchone()
            assert npc_move and c.execute('SELECT status FROM cross_layer_passages WHERE id=?', (pid,)).fetchone()[0] == 'pending'
            assert c.execute('SELECT space_layer FROM characters WHERE id=?', (e['first'],)).fetchone()[0] == 'ground'
            assert not c.execute("SELECT 1 FROM character_movements WHERE world_id=? AND character_id=? AND status='moving'", (e['wid'],e['player'])).fetchone()
        player = client.post(base + f'/cross-layer/{pid}/approach', json={'idempotency_key': 'http-player-approach'})
        assert player.status_code == 200 and player.json()['approach_status'] == 'applied', player.text
        retry = client.post(base + f'/cross-layer/{pid}/approach', json={'idempotency_key': 'http-player-approach'})
        assert retry.status_code == 200 and retry.json()['movement_id'] == player.json()['movement_id']
        with e['db'].write() as c:
            previous = at(c,e)
            arrive(c,e,previous,[npc_move[0],player.json()['movement_id']])
            assert c.execute('SELECT status FROM cross_layer_passages WHERE id=?', (pid,)).fetchone()[0] == 'pending'

        def respond_traverse(**kwargs):
            contexts.append(kwargs['context'])
            return NpcReply.model_validate({'reply': '我决定自己先通过。', 'action_intent':
                {'kind': 'cross_layer', 'target_id': pid, 'operation': 'enter'}})
        model.respond_to_player = respond_traverse
        second = client.post(base + '/player/act', json={'intent': '说：你现在愿意自行通过吗？', 'target_character_id': e['first']})
        assert second.status_code == 201, second.text
        assert contexts[-1]['scene']['cross_layer_passages'][0]['npc_at_node']
        with e['db'].read() as c:
            assert c.execute('SELECT status FROM cross_layer_passages WHERE id=?', (pid,)).fetchone()[0] == 'npc_passed'
            assert c.execute('SELECT space_layer FROM characters WHERE id=?', (e['player'],)).fetchone()[0] == 'ground'
        final = client.post(base + f'/cross-layer/{pid}/traverse')
        assert final.status_code == 200 and final.json()['status'] == 'completed', final.text
        assert final.json()['player_event_id'] != final.json()['npc_event_id']
        with e['db'].read() as c:
            positions = {row['id']: row['space_layer'] for row in c.execute('SELECT id,space_layer FROM characters WHERE id IN (?,?)', (e['player'],e['first']))}
            assert positions == {e['player']: e['city']['space_layer'], e['first']: e['city']['space_layer']}
