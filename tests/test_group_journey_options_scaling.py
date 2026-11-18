"""哥哥，地点候选按本人已知事件一次求出，不能对每个陌生地点重复扫描全部记忆。"""

# ruff: noqa: F811
from uuid import uuid4

from test_group_journey_text_api import trio  # noqa: F401
from test_lived_world import town  # noqa: F401

from world_engine.group_journey_options import group_journey_options
from world_engine.repository import to_iso


def test_many_unknown_places_and_known_events_keep_exact_candidates_with_bounded_database_work(trio):
    e = trio
    with e.db.write() as c:
        expected = group_journey_options(c, e.wid, e.player, e.at)
        # 哥哥，这些都是独立负载夹具，正式世界不写测试地点或记忆。
        c.executemany("INSERT INTO locations(id,world_id,name,kind,resources_json) VALUES (?,?,?,'field','{}')",
                      [(str(uuid4()), e.wid, f"未到访负载地点{index:04d}") for index in range(900)])
        events = [str(uuid4()) for _ in range(1000)]
        c.executemany(
            "INSERT INTO world_events(id,world_id,tick_id,occurred_at,event_type,actor_id,location_id,summary,payload_json,created_at) "
            "VALUES (?,?,?,?,'knowledge.load_fixture',?,?,'当前地点的既有观察','{}',?)",
            [(eid, e.wid, eid, to_iso(e.at), e.player, e.loc["id"], to_iso(e.at)) for eid in events])
        c.executemany("INSERT INTO event_observers(event_id,character_id,channel) VALUES (?,?,'participant')",
                      [(eid, e.player) for eid in events])
        c.executemany("INSERT INTO character_event_knowledge VALUES (?,?,?,'experienced',NULL,1,0,?)",
                      [(e.player, eid, e.wid, to_iso(e.at)) for eid in events])
        steps = 0
        def bounded():
            nonlocal steps
            steps += 1
            return steps > 200
        c.set_progress_handler(bounded, 1000)
        try:
            actual = group_journey_options(c, e.wid, e.player, e.at)
        finally:
            c.set_progress_handler(None, 0)
        assert actual == expected
        assert steps <= 200
