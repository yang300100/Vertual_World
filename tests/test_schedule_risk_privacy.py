"""哥哥，人物自己评估赴约风险不等于已经告知对方。"""

import pytest

from test_lived_world import town  # noqa: F401
from test_schedules import meeting
from test_travel_interaction import ContactModel, run
from world_engine.schedules import ScheduleService
from world_engine.society import SocietyService


@pytest.mark.parametrize("scene", ["near", "away", "between_locations"])
def test_private_risk_uses_actors_actual_location_without_auto_notification(town, scene):
    e = town
    contract_id = meeting(e)
    with e.db.write() as c:
        if scene == "away":
            place = c.execute("SELECT * FROM locations WHERE world_id=? AND id<>? AND is_active=1 LIMIT 1", (e.wid, e.loc["id"])).fetchone()
            assert place
            # 哥哥，这只是隔离场景的起始布置，之后不改写任何移动结果。
            c.execute("UPDATE characters SET longitude=?,latitude=?,current_location_id=?,location_id=? WHERE id=?",
                      (place["longitude"], place["latitude"], place["id"], place["id"], e.npc))
        elif scene == "between_locations":
            c.execute("UPDATE characters SET current_location_id=NULL WHERE id=?", (e.npc,))
        c.execute("UPDATE characters SET satiety=0 WHERE id=?", (e.npc,))
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        plan = ScheduleService.assess(c, npc, e.at)
        assert plan and plan["contract"]["request_id"] == contract_id and plan["reason"]
        assert ScheduleService.record_assessment(c, npc, e.at, plan)
        event_id = c.execute("SELECT source_event_id FROM npc_schedule_assessments WHERE character_id=?", (e.npc,)).fetchone()[0]
        event = c.execute("SELECT * FROM world_events WHERE id=?", (event_id,)).fetchone()
        assert event["actor_id"] == e.npc and event["target_id"] is None
        assert event["location_id"] == npc["current_location_id"]
        assert c.execute("SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?", (event_id, e.npc)).fetchone()
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?", (event_id, e.player)).fetchone()
        assert not c.execute("SELECT 1 FROM event_observers WHERE event_id=? AND character_id=?", (event_id, e.player)).fetchone()
        assert not c.execute("SELECT 1 FROM player_notifications WHERE event_id=?", (event_id,)).fetchone()
        assert c.execute("SELECT status FROM contract_fulfillments WHERE request_id=?", (contract_id,)).fetchone()[0] == "active"


@pytest.mark.parametrize("should_contact", [False, True])
def test_nearby_risk_only_reaches_player_after_npc_independently_contacts(town, should_contact):
    e = town
    contract_id = meeting(e)
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=0 WHERE id=?", (e.npc,))
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        assert ScheduleService.record_assessment(c, npc, e.at, ScheduleService.assess(c, npc, e.at))
        source = c.execute("SELECT source_event_id FROM npc_schedule_assessments WHERE character_id=?", (e.npc,)).fetchone()[0]
        SocietyService.tick(c, e.wid, e.at)
        job = c.execute("SELECT id,source_event_id FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? AND status='pending'", (e.wid, e.npc, e.player)).fetchone()
        assert job and job["source_event_id"] == source
        assert not c.execute("SELECT 1 FROM player_notifications WHERE recipient_id=? AND event_id=?", (e.player, source)).fetchone()
    model = ContactModel(should_contact)
    assert run(e, model) == int(should_contact)
    with e.db.read() as c:
        result = c.execute("SELECT status,result_event_id FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()
        assert result["status"] == ("done" if should_contact else "declined")
        assert bool(result["result_event_id"]) == should_contact
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?", (source, e.player)).fetchone()
        notification = c.execute("SELECT event_id FROM player_notifications WHERE id=? AND recipient_id=?", (job["id"], e.player)).fetchone()
        if should_contact:
            assert notification and notification["event_id"] == result["result_event_id"]
            assert c.execute("SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?", (notification["event_id"], e.player)).fetchone()
        else:
            assert notification is None
        assert c.execute("SELECT status FROM contract_fulfillments WHERE request_id=?", (contract_id,)).fetchone()[0] == "active"
