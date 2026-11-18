"""哥哥，这些交流与回应位置测试只使用隔离世界。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_rides import ride_world, current, boarded, choose, advance, arrival_seconds  # noqa: F401
from test_ride_recovery import blocked
from test_explicit_player_actions import ObservingProvider
from world_engine.actions import ActionService
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.player_action_flow import react_to_action


def social(e, c):
    return ActionService().execute(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=current(e, c),
        proposal=ActionProposal(actor_id=e.player, action=ActionType.SOCIALIZE, target_id=e.npc,
                                reason='隔离验收', dialogue='车内对话'))


@pytest.mark.parametrize('phase',['moving','blocked'])
def test_aboard_social_records_physical_location_and_real_witnesses(ride_world,phase):
    e=ride_world
    if phase=='blocked':
        blocked(e, party=[e.player,e.npc,e.other])
    else:
        rid=boarded(e,member_ids=[e.player,e.npc,e.other])
        choose(e,e.player,'depart',rid)
        advance(e,arrival_seconds(e,rid)/2)
    with e.db.write() as c:
        assert c.execute('SELECT current_location_id FROM vehicle_states WHERE vehicle_id=?',(e.vehicle,)).fetchone()[0] is None
        result=social(e,c)
        assert result.accepted
        event=c.execute('SELECT * FROM world_events WHERE id=?',(result.event_id,)).fetchone()
        assert event['location_id'] is None
        assert not c.execute("SELECT 1 FROM event_affected_entities WHERE event_id=? AND entity_type='location'",(result.event_id,)).fetchone()
        observers={r['character_id']:r['channel'] for r in c.execute('SELECT * FROM event_observers WHERE event_id=?',(result.event_id,))}
        assert observers[e.player]=='participant' and observers[e.npc]=='participant'
        assert observers[e.other]=='heard' and e.outside not in observers


def test_off_vehicle_social_retains_existing_location_fallback(ride_world):
    e=ride_world
    with e.db.write() as c:
        c.execute('UPDATE characters SET current_location_id=NULL WHERE id=?',(e.player,))
        result=social(e,c)
        assert result.accepted
        assert c.execute('SELECT location_id FROM world_events WHERE id=?',(result.event_id,)).fetchone()[0]==e.origin


def test_inconsistent_vehicle_rejects_social_without_consuming_needs(ride_world):
    e=ride_world;blocked(e)
    with e.db.write() as c:
        before=tuple(c.execute('SELECT energy,satiety FROM characters WHERE id=?',(e.player,)).fetchone())
        count=c.execute("SELECT COUNT(*) FROM world_events WHERE event_type='action.socialize'").fetchone()[0]
        c.execute('UPDATE vehicle_states SET current_location_id=? WHERE vehicle_id=?',(e.origin,e.vehicle))
        result=social(e,c)
        assert not result.accepted and '位置不一致' in result.rejection_reason
        assert tuple(c.execute('SELECT energy,satiety FROM characters WHERE id=?',(e.player,)).fetchone())==before
        assert c.execute("SELECT COUNT(*) FROM world_events WHERE event_type='action.socialize'").fetchone()[0]==count


def pending_reaction(e):
    with e.db.write() as c:
        result=social(e,c)
        assert result.accepted
        c.execute("INSERT INTO player_action_reactions(source_event_id,world_id,npc_id,status) VALUES (?,?,?,'pending')",(result.event_id,e.wid,e.npc))
    provider=ObservingProvider(e.db)
    return result.event_id,WorldEngine(e.db,e.settings,decision_provider=provider)


def test_real_reaction_uses_current_vehicle_location(ride_world):
    e=ride_world;blocked(e,party=[e.player,e.npc,e.other])
    source,engine=pending_reaction(e)
    reply=react_to_action(engine,e.wid,source)
    assert reply.get('event_id'),reply
    with e.db.read() as c:
        event=c.execute('SELECT * FROM world_events WHERE id=?',(reply['event_id'],)).fetchone()
        assert event['actor_id']==e.npc and event['target_id']==e.player and event['location_id'] is None
        assert not c.execute("SELECT 1 FROM event_affected_entities WHERE event_id=? AND entity_type='location'",(event['id'],)).fetchone()
        assert {r[0] for r in c.execute('SELECT character_id FROM event_observers WHERE event_id=?',(event['id'],))}=={e.npc,e.player}


def test_reaction_location_failure_rolls_back_settlement(ride_world,monkeypatch):
    e=ride_world;blocked(e)
    source,engine=pending_reaction(e)
    with e.db.write() as c:
        c.execute('UPDATE vehicle_states SET current_location_id=? WHERE vehicle_id=?',(e.origin,e.vehicle))
    marker=str(uuid4())
    def staged(c,**kwargs):
        c.execute("UPDATE characters SET energy=energy-10 WHERE id=?",(e.npc,))
        c.execute("INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,summary,payload_json,created_at) SELECT ?,world_id,?,occurred_at,'test.sentinel','隔离哨兵','{}',created_at FROM world_events WHERE id=?",(marker,marker,source))
        return SimpleNamespace(reply='隔离回应',stage_direction=None,action_status=None,action_event_id=None)
    monkeypatch.setattr('world_engine.player_action_flow.settle_npc_reply',staged)
    with e.db.read() as c:energy=c.execute('SELECT energy FROM characters WHERE id=?',(e.npc,)).fetchone()[0]
    assert react_to_action(engine,e.wid,source).get('error')
    with e.db.read() as c:
        assert c.execute('SELECT energy FROM characters WHERE id=?',(e.npc,)).fetchone()[0]==energy
        assert not c.execute('SELECT 1 FROM world_events WHERE id=?',(marker,)).fetchone()
        assert c.execute('SELECT status FROM player_action_reactions WHERE source_event_id=?',(source,)).fetchone()[0]=='failed'
