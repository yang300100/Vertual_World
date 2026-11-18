"""哥哥，单地点的快速查询仍只能使用本人真实知情且有权限的经历。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from test_lived_world import town  # noqa: F401
from test_npc_food_recovery import prepare_hungry
from world_engine.daily_life import DailyLifeService
from world_engine.repository import to_iso


@pytest.mark.parametrize(
    'condition',
    ['unmodified', 'future_event', 'future_knowledge', 'missing_knowledge', 'wrong_source',
     'wrong_channel', 'other_owner', 'bound_elsewhere', 'inactive', 'other_layer'],
)
def test_single_place_requires_current_personal_knowledge_and_permission(town, condition):
    e = town
    prepare_hungry(e, known_food=True)
    with e.db.write() as c:
        event = c.execute("SELECT * FROM world_events WHERE actor_id=? AND event_type='action.resource_harvested'", (e.npc,)).fetchone()
        item = json.loads(event['payload_json'])['item_type_id']
        actor = c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone()
        assert DailyLifeService.known_food_places(c, actor, e.at, [e.loc['id']]) == [e.loc['id']]
        if condition == 'future_event':
            c.execute('UPDATE world_events SET occurred_at=? WHERE id=?', (to_iso(e.at + timedelta(hours=1)), event['id']))
        elif condition == 'future_knowledge':
            c.execute('UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=? AND character_id=?',
                      (to_iso(e.at + timedelta(hours=1)), event['id'], e.npc))
        elif condition == 'missing_knowledge':
            c.execute('DELETE FROM character_event_knowledge WHERE event_id=? AND character_id=?', (event['id'], e.npc))
        elif condition == 'wrong_source':
            c.execute("UPDATE character_event_knowledge SET source_kind='reported' WHERE event_id=? AND character_id=?", (event['id'], e.npc))
        elif condition == 'wrong_channel':
            c.execute("UPDATE event_observers SET channel='heard' WHERE event_id=? AND character_id=?", (event['id'], e.npc))
        elif condition == 'other_owner':
            c.execute('UPDATE world_item_profiles SET resource_owner_id=? WHERE item_type_id=?', (e.player, item))
        elif condition == 'bound_elsewhere':
            other = c.execute('SELECT id FROM locations WHERE world_id=? AND id!=? LIMIT 1', (e.wid, e.loc['id'])).fetchone()[0]
            c.execute('UPDATE world_item_profiles SET resource_location_id=? WHERE item_type_id=?', (other, item))
        elif condition == 'inactive':
            c.execute('UPDATE locations SET is_active=0 WHERE id=?', (e.loc['id'],))
        elif condition == 'other_layer':
            c.execute("UPDATE locations SET space_layer='sky' WHERE id=?", (e.loc['id'],))
        expected = [e.loc['id']] if condition == 'unmodified' else []
        assert DailyLifeService.known_food_places(c, actor, e.at, [e.loc['id']]) == expected


def test_multiple_places_keep_previous_query_order(town):
    e = town
    prepare_hungry(e, known_food=True)
    with e.db.write() as c:
        event = dict(c.execute("SELECT * FROM world_events WHERE actor_id=? AND event_type='action.resource_harvested'", (e.npc,)).fetchone())
        item = json.loads(event['payload_json'])['item_type_id']
        c.execute('UPDATE world_item_profiles SET resource_location_id=NULL WHERE item_type_id=?', (item,))
        place = dict(c.execute('SELECT * FROM locations WHERE id=?', (e.loc['id'],)).fetchone())
        new_place, new_event = str(uuid4()), str(uuid4())
        place.update(id=new_place, name='隔离的第二个亲历地点')
        keys = list(place)
        c.execute(f"INSERT INTO locations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(place[k] for k in keys))
        original_event = event['id']
        event.update(id=new_event, location_id=new_place)
        keys = list(event)
        c.execute(f"INSERT INTO world_events ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(event[k] for k in keys))
        for table in ('character_event_knowledge', 'event_observers'):
            record = dict(c.execute(f'SELECT * FROM {table} WHERE event_id=? AND character_id=?', (original_event, e.npc)).fetchone())
            record['event_id'] = new_event
            keys = list(record)
            c.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(record[k] for k in keys))
        actor = c.execute('SELECT * FROM characters WHERE id=?', (e.npc,)).fetchone()
        candidates = [new_place, e.loc['id']]
        actual = DailyLifeService.known_food_places(c, actor, e.at, candidates)
        assert len(actual) == 2 and set(actual) == set(candidates)
        assert actual == DailyLifeService.known_food_places(c, actor, e.at, list(reversed(candidates)))
