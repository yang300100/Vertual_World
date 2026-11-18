"""哥哥，错过约定只给本人一次可拒绝的后续考虑，不替双方作决定。"""

from datetime import timedelta
from uuid import uuid4

import pytest

from test_appointments import choose, get, offer
from test_lived_world import town  # noqa: F401
from test_travel_interaction import ContactModel, run
from world_engine.appointment_followup import AppointmentFollowupService
from world_engine.appointments import AppointmentService
from world_engine.repository import from_iso, to_iso
from world_engine.society import SocietyService


def missed(e, *, used_today=False):
    appointment = offer(e)
    assert choose(e, "accept", appointment).status_code == 200
    if used_today:
        with e.db.write() as c:
            SocietyService.tick(c, e.wid, e.at)
            c.execute("DELETE FROM npc_outreach_jobs WHERE npc_id<>?", (e.npc,))
        assert run(e, ContactModel(False)) == 0
    with e.db.write() as c:
        row = c.execute("SELECT * FROM social_appointments WHERE id=?", (appointment,)).fetchone()
        at = from_iso(row["ends_at"])
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))
        AppointmentService.maintain(c, e.wid, at)
        row = c.execute("SELECT * FROM social_appointments WHERE id=?", (appointment,)).fetchone()
        assert row["status"] == "missed"
        source = row["source_event_id"]
    return appointment, source, at


def queue(e, at):
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))
        SocietyService.tick(c, e.wid, at)
        c.execute("DELETE FROM npc_outreach_jobs WHERE npc_id<>?", (e.npc,))
        row = c.execute("SELECT * FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? AND world_day=?",
                        (e.wid, e.npc, e.player, at.date().isoformat())).fetchone()
        return dict(row) if row else None


def test_used_daily_opportunity_preserves_old_result_then_considers_next_day(town):
    e = town
    appointment, source, at = missed(e, used_today=True)
    old = queue(e, at)
    assert old and old["status"] == "declined" and old["source_event_id"] != source
    job = queue(e, at + timedelta(days=1))
    assert job and job["status"] == "pending" and job["source_event_id"] == source
    assert job["reason"].startswith(AppointmentFollowupService.JOB_PREFIX)
    assert run(e, ContactModel(False)) == 0
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "declined"
        assert c.execute("SELECT status FROM social_appointments WHERE id=?", (appointment,)).fetchone()[0] == "missed"
        assert not c.execute("SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)).fetchone()
    later = queue(e, at + timedelta(days=2))
    assert later is None or later["source_event_id"] != source


def test_npc_can_propose_repair_but_player_must_accept(town):
    e = town
    appointment, source, at = missed(e)
    job = queue(e, at)
    assert job and job["source_event_id"] == source
    model = ContactModel(True, appointment_intent={
        "operation": "propose", "kind": "talk", "location_id": e.loc["id"],
        "meeting_world_time": to_iso(at + timedelta(hours=2)), "minutes": 15,
        "repairs_id": appointment, "statement": "我们另约时间见面，你愿意吗？",
    })
    assert run(e, model) == 1
    assert model.context["decision"]["may_decline"]
    assert "不能猜测对方" in model.context["decision"]["opportunity"]
    with e.db.read() as c:
        row = c.execute("SELECT * FROM social_appointments WHERE repairs_id=? ORDER BY rowid DESC LIMIT 1", (appointment,)).fetchone()
        assert row and row["status"] == "pending" and row["proposer_id"] == e.npc and row["recipient_id"] == e.player
        repair_id = row["id"]
        assert c.execute("SELECT status,repaired_by FROM social_appointments WHERE id=?", (appointment,)).fetchone()[:] == ("missed", None)
    assert choose(e, "accept", repair_id).status_code == 200
    assert get(e, repair_id)["status"] == "accepted"
    assert get(e, appointment)["status"] == "missed" and get(e, appointment)["repaired_by"] is None


@pytest.mark.parametrize("fault", ["unknown_confirmation", "future_knowledge", "forged_event", "already_repairing"])
def test_unverified_or_obsolete_source_never_offers_followup(town, fault):
    e = town
    appointment, source, at = missed(e)
    with e.db.write() as c:
        accepted = c.execute("SELECT id FROM world_events WHERE world_id=? AND event_type='social.appointment_accepted' "
            "AND json_extract(payload_json,'$.appointment_id')=?", (e.wid, appointment)).fetchone()[0]
        if fault == "unknown_confirmation":
            c.execute("DELETE FROM character_event_knowledge WHERE character_id=? AND event_id=?", (e.npc, accepted))
        elif fault == "future_knowledge":
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE character_id=? AND event_id=?", (to_iso(at + timedelta(days=1)), e.npc, accepted))
        elif fault == "forged_event":
            c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (source,))
        else:
            c.execute("INSERT INTO social_appointments(id,world_id,proposer_id,recipient_id,kind,status,location_id,starts_at,ends_at,minutes,statement,proposed_at,repairs_id,source_event_id) "
                      "VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)", (str(uuid4()), e.wid, e.npc, e.player, "talk", e.loc["id"], to_iso(at + timedelta(hours=2)), to_iso(at + timedelta(hours=3)), 15, "隔离测试", to_iso(at), appointment, source))
        assert AppointmentFollowupService.opportunity(c, e.wid, e.npc, e.player, at) is None


def test_source_invalidated_during_model_call_cannot_deliver(town):
    e = town
    _, source, at = missed(e)
    job = queue(e, at)
    assert job and job["source_event_id"] == source
    def remove_source():
        with e.db.write() as c:
            c.execute("UPDATE world_events SET payload_json='{}' WHERE id=?", (source,))
    assert run(e, ContactModel(True, callback=remove_source)) == 0
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert not c.execute("SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)).fetchone()


@pytest.mark.parametrize("when", ["before_claim", "during_model"])
def test_expired_letter_permission_cannot_deliver_followup(town, when):
    """哥哥，信笺许可失效后不能用旧任务联系，也不替模型作决定。"""
    e = town
    _, source, at = missed(e)
    contact = str(uuid4())
    with e.db.write() as c:
        # 哥哥，测试初始场景的两人相距足够远；并不修改正式世界的位置。
        c.execute("UPDATE characters SET longitude=longitude+0.1 WHERE id=?", (e.npc,))
        c.execute("INSERT INTO character_contacts(id,world_id,requester_id,recipient_id,status,created_at,updated_at) VALUES (?,?,?,?,'accepted',?,?)",
                  (contact, e.wid, e.player, e.npc, to_iso(at), to_iso(at)))
    job = queue(e, at)
    assert job and job["source_event_id"] == source and job["channel"] == "letter"
    def revoke():
        with e.db.write() as c:
            c.execute("UPDATE character_contacts SET status='rejected' WHERE id=?", (contact,))
    model = ContactModel(True, callback=revoke if when == "during_model" else None)
    if when == "before_claim":
        revoke()
    assert run(e, model) == 0
    if when == "before_claim":
        assert model.context is None
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert not c.execute("SELECT 1 FROM character_messages WHERE contact_id=?", (contact,)).fetchone()
        assert not c.execute("SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)).fetchone()


def test_followup_respects_world_daily_delivery_limit(town):
    """哥哥，错约考虑不能绕过世界每天原有的成功投递上限。"""
    e = town
    _, source, at = missed(e)
    job = queue(e, at)
    assert job and job["source_event_id"] == source
    with e.db.write() as c:
        template = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone())
        keys = list(template)
        for index in range(4):
            clone = {**template, "id": str(uuid4()), "name": f"隔离联系者{index}", "is_player": 0, "is_pov": 0}
            c.execute("INSERT INTO characters(" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")",
                      tuple(clone[key] for key in keys))
            c.execute("INSERT INTO npc_outreach_jobs(id,world_id,npc_id,player_id,channel,reason,world_day,status) VALUES (?,?,?,?,'in_person','隔离已投递记录',?,'done')",
                      (str(uuid4()), e.wid, clone["id"], e.player, at.date().isoformat()))
    model = ContactModel(True)
    assert run(e, model) == 0 and model.context is None
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "pending"
        assert not c.execute("SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)).fetchone()


def test_many_unrelated_npcs_do_not_trigger_per_person_followup_query(town, monkeypatch):
    """哥哥，人物很多但没有共同错约时，不应对每个人重复查询错约来源。"""
    e = town
    with e.db.write() as c:
        row = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone())
        keys = list(row)
        for index in range(160):
            clone = {**row, "id": str(uuid4()), "name": f"隔离居民{index}",
                     "longitude": row["longitude"] + 0.1, "is_player": 0, "is_pov": 0}
            c.execute("INSERT INTO characters(" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")",
                      tuple(clone[key] for key in keys))
        calls = []
        original = AppointmentFollowupService.opportunity.__func__
        def tracked(cls, connection, world_id, npc_id, player_id, at):
            calls.append(npc_id)
            return original(cls, connection, world_id, npc_id, player_id, at)
        monkeypatch.setattr(AppointmentFollowupService, "opportunity", classmethod(tracked))
        SocietyService.tick(c, e.wid, e.at)
        assert calls == []


def test_unrelated_npc_cannot_receive_or_infer_another_pairs_missed_appointment(town):
    """哥哥，即使旁人存在，也不能成为他人旧约的后续联系者。"""
    e = town
    appointment, source, at = missed(e)
    with e.db.write() as c:
        row = dict(c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone())
        unrelated = str(uuid4())
        keys = list(row)
        row.update(id=unrelated, name="隔离旁人", longitude=row["longitude"] + 0.1, is_player=0, is_pov=0)
        c.execute("INSERT INTO characters(" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")",
                  tuple(row[key] for key in keys))
        assert AppointmentFollowupService._valid(c, e.wid, unrelated, e.player, source, at) is None
        assert AppointmentFollowupService.opportunity(c, e.wid, unrelated, e.player, at) is None
        SocietyService.tick(c, e.wid, at)
        assert not c.execute("SELECT 1 FROM npc_outreach_jobs WHERE npc_id=? AND source_event_id=?", (unrelated, source)).fetchone()
        assert not c.execute("SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?", (unrelated, source)).fetchone()
