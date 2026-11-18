"""哥哥，现场与信笺都要由本人选择，私人判断不会直接变成玩家见闻。"""

import pytest

from test_lived_world import post, town  # noqa: F401
from test_schedules import meeting
from test_travel_interaction import ContactModel, run
from world_engine.schedules import ScheduleService
from world_engine.society import SocietyService


def make_remote_risk(e):
    contract_id = meeting(e)
    with e.db.write() as c:
        # 哥哥，隔离场景的初始布置只验证真实远距，不改任何正式存档。
        other = c.execute("SELECT * FROM locations WHERE world_id=? AND id<>? AND is_active=1 ORDER BY id LIMIT 1", (e.wid, e.loc["id"])).fetchone()
        assert other
        c.execute("UPDATE characters SET longitude=?,latitude=?,location_id=?,current_location_id=?,satiety=0 WHERE id=?",
                  (other["longitude"], other["latitude"], other["id"], other["id"], e.npc))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        assessment = ScheduleService.assess(c, actor, e.at)
        assert assessment and assessment["reason"]
        assert ScheduleService.record_assessment(c, actor, e.at, assessment)
        source = c.execute("SELECT source_event_id FROM npc_schedule_assessments WHERE character_id=?", (e.npc,)).fetchone()[0]
    return contract_id, source


def test_remote_private_risk_never_appears_in_player_experience_or_log(town):
    e = town
    contract_id, source = make_remote_risk(e)
    experience = e.client.get(f"/api/worlds/{e.wid}/player/experience")
    assert experience.status_code == 200
    assert source not in {item["id"] for item in experience.json()["events"]}
    assert source not in {item["event_id"] for item in experience.json()["notifications"]}
    log = e.client.get(f"/api/worlds/{e.wid}/events", params={"scope":"log", "participant_id":e.player, "limit":200})
    assert log.status_code == 200 and source not in {item["id"] for item in log.json()}
    assert e.client.get(f"/api/worlds/{e.wid}/player/events/{source}/causes").status_code == 404
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        assert not c.execute("SELECT 1 FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? AND source_event_id=?", (e.wid,e.npc,e.player,source)).fetchone()
        assert c.execute("SELECT status FROM contract_fulfillments WHERE request_id=?", (contract_id,)).fetchone()[0] == "active"


@pytest.mark.parametrize("should_contact", [False, True])
def test_remote_risk_with_consented_letters_requires_npc_choice(town, should_contact):
    e = town
    contact = post(e, "contacts", {"recipient_id": e.npc})
    assert contact["status"] == "accepted"
    contract_id, source = make_remote_risk(e)
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        job = c.execute("SELECT * FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? AND source_event_id=?", (e.wid,e.npc,e.player,source)).fetchone()
        assert job and job["channel"] == "letter" and job["contact_id"] == contact["contact_id"]
        job_id = job["id"]
    assert run(e, ContactModel(should_contact)) == int(should_contact)
    with e.db.read() as c:
        job = c.execute("SELECT status,result_event_id FROM npc_outreach_jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == ("done" if should_contact else "declined")
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE event_id=? AND character_id=?", (source,e.player)).fetchone()
        notification = c.execute("SELECT event_id FROM player_notifications WHERE id=? AND recipient_id=?", (job_id,e.player)).fetchone()
        assert bool(notification) == should_contact
        if notification:
            assert notification["event_id"] == job["result_event_id"] != source
        assert c.execute("SELECT status FROM contract_fulfillments WHERE request_id=?", (contract_id,)).fetchone()[0] == "active"


def test_remote_risk_does_not_use_revoked_letter_channel(town):
    e = town
    contact = post(e, "contacts", {"recipient_id": e.npc})
    assert contact["status"] == "accepted"
    _, source = make_remote_risk(e)
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        job = c.execute("SELECT id FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? AND source_event_id=?", (e.wid,e.npc,e.player,source)).fetchone()
        assert job
        job_id = job["id"]
        # 哥哥，模拟本人已撤销原通信许可；不能把旧队列当作新同意。
        c.execute("UPDATE character_contacts SET status='rejected' WHERE id=?", (contact["contact_id"],))
    model = ContactModel(True)
    assert run(e, model) == 0 and model.context is None
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?", (job_id,)).fetchone()[0] == "cancelled"
        assert not c.execute("SELECT 1 FROM player_notifications WHERE id=?", (job_id,)).fetchone()
        assert not c.execute("SELECT 1 FROM character_messages WHERE id=?", (job_id,)).fetchone()
