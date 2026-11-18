"""哥哥，本次真实见面只提供一次可拒绝的考虑，不按性格阈值替本人决定。"""

# ruff: noqa: F811
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_companion_consent import choose as companion_choice
from test_encounter_episodes import damage_source
from test_intimacy import choose as relationship_choice
from test_lived_world import town  # noqa: F401
from test_relationship_continuity import remember
from test_travel_interaction import ContactModel, job_status, journey, run, state

from world_engine.database import Database
from world_engine.encounters import EncounterService
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso
from world_engine.society import SocietyService
from world_engine.travel_interaction import TravelInteractionService


def tick(e, at=None):
    with e.db.write() as c:
        if at is not None:
            c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), e.wid))
        at = from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
        )
        SocietyService.tick(c, e.wid, at)
        c.execute("DELETE FROM npc_outreach_jobs WHERE npc_id!=?", (e.npc,))
        row = c.execute(
            "SELECT * FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (e.wid, e.npc, e.player),
        ).fetchone()
        return dict(row) if row else None


def current(e, at=None):
    with e.db.read() as c:
        at = at or from_iso(
            c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0]
        )
        return EncounterService.current(c, e.wid, e.npc, e.player, at)


def facts(e):
    with e.db.read() as c:
        return {
            "relations": [
                tuple(r)
                for r in c.execute(
                    "SELECT * FROM relationships WHERE source_character_id=? AND target_character_id=?",
                    (e.npc, e.player),
                )
            ],
            "acquaintance": [
                tuple(r)
                for r in c.execute(
                    "SELECT * FROM character_acquaintances WHERE observer_id=? AND subject_id=?",
                    (e.npc, e.player),
                )
            ],
            "sessions": c.execute("SELECT COUNT(*) FROM companion_sessions").fetchone()[0],
            "positions_resources": state(e),
        }


@pytest.mark.parametrize("traits,score", [("谨慎", -100), ("热情", 100), ("沉静", 0)])
@pytest.mark.parametrize("should_contact", [False, True])
def test_actual_encounter_offers_choice_for_different_people_without_predetermining_contact(
    town,
    traits,
    score,
    should_contact,
):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET traits_json=? WHERE id=?", (json.dumps([traits]), e.npc))
        c.execute(
            "INSERT OR REPLACE INTO relationships VALUES (?,?,?,?,?,?)",
            (e.wid, e.npc, e.player, score, score, to_iso(e.at)),
        )
    job = tick(e)
    binding = current(e)
    assert job and binding and binding["offered_job_id"] == job["id"]
    assert job["reason"].startswith(EncounterService.JOB_PREFIX)
    assert job["source_event_id"] == binding["source_event_id"]
    before = facts(e)
    model = ContactModel(should_contact)
    assert run(e, model) == int(should_contact)
    assert job_status(e, job) == ("done" if should_contact else "declined")
    assert model.context["current_encounter"]["episode_id"] == binding["episode_id"]
    assert model.context["decision"]["may_decline"]
    if not should_contact:
        assert facts(e) == before
    else:
        assert facts(e)["sessions"] == before["sessions"] == 0
    assert run(e, ContactModel()) == 0


def test_continuous_meeting_does_not_replay_first_encounter_or_gain_extra_daily_contact(town):
    e = town
    job = tick(e)
    initial = current(e)
    assert run(e, ContactModel(False)) == 0
    for minutes in (1, 20, 59):
        tick(e, e.at + timedelta(minutes=minutes))
        assert current(e)["episode_id"] == initial["episode_id"]
    assert job_status(e, job) == "declined"
    tick(e, e.at + timedelta(days=1))
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_events WHERE world_id=? AND actor_id=? AND target_id=? "
                "AND event_type='action.encounter'",
                (e.wid, e.npc, e.player),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM npc_outreach_jobs WHERE world_id=? AND npc_id=? AND player_id=?",
                (e.wid, e.npc, e.player),
            ).fetchone()[0]
            == 1
        )
        assert not EncounterService.opportunity(c, e.wid, e.npc, e.player, e.at + timedelta(days=1))


def test_real_separation_then_reunion_can_offer_next_day_without_making_a_new_person(town):
    e = town
    first = tick(e)
    old = current(e)
    run(e, ContactModel(False))
    with e.db.write() as c:
        people_before = c.execute(
            "SELECT COUNT(*) FROM characters WHERE world_id=?", (e.wid,)
        ).fetchone()[0]
        c.execute("UPDATE characters SET longitude=longitude+0.001 WHERE id=?", (e.npc,))
    tick(e, e.at + timedelta(minutes=5))
    assert current(e) is None
    with e.db.write() as c:
        c.execute(
            "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
            (e.loc["longitude"], e.loc["latitude"], e.npc),
        )
    second = tick(e, e.at + timedelta(days=1))
    binding = current(e)
    assert binding["episode_id"] != old["episode_id"]
    assert second["id"] != first["id"] and second["source_event_id"] != first["source_event_id"]
    model = ContactModel(False)
    assert run(e, model) == 0
    assert model.context["current_encounter"]["kind"] == "reunion"
    with e.db.read() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM characters WHERE world_id=?", (e.wid,)).fetchone()[0]
            == people_before
        )


@pytest.mark.parametrize("change", ["distance", "layer", "room", "body"])
def test_real_scene_change_cancels_the_old_opportunity_before_model(town, change):
    e = town
    job = tick(e)
    rid = None
    if change == "room":
        from test_appointment_rooms import room

        rid = room(e)
    with e.db.write() as c:
        if change == "distance":
            c.execute("UPDATE characters SET longitude=longitude+0.0004 WHERE id=?", (e.npc,))
        elif change == "layer":
            c.execute("UPDATE characters SET space_layer='upper' WHERE id=?", (e.npc,))
        elif change == "room":
            c.execute("UPDATE characters SET current_room_id=? WHERE id=?", (rid, e.npc))
        else:
            c.execute("UPDATE characters SET health=0 WHERE id=?", (e.npc,))
    tick(e, e.at + timedelta(seconds=1))
    assert current(e) is None
    before = state(e)
    model = ContactModel()
    assert run(e, model) == 0 and model.context is None
    assert job_status(e, job) == "cancelled" and state(e) == before


@pytest.mark.parametrize("change", ["distance", "new_episode", "expired"])
def test_response_during_scene_change_does_not_deliver_or_create_consent(town, change):
    e = town
    job = tick(e)
    before = facts(e)

    def callback():
        with e.db.write() as c:
            if change == "expired":
                c.execute(
                    "UPDATE worlds SET current_time=? WHERE id=?",
                    (to_iso(e.at + timedelta(minutes=60)), e.wid),
                )
            else:
                c.execute("UPDATE characters SET longitude=longitude+0.001 WHERE id=?", (e.npc,))
                EncounterService.observe_pair(c, e.wid, e.npc, e.player, e.at)
                if change == "new_episode":
                    c.execute(
                        "UPDATE characters SET longitude=?,latitude=? WHERE id=?",
                        (e.loc["longitude"], e.loc["latitude"], e.npc),
                    )
                    EncounterService.observe_pair(c, e.wid, e.npc, e.player, e.at)

    model = ContactModel(callback=callback)
    assert run(e, model) == 0 and model.context
    assert job_status(e, job) == "cancelled" and facts(e)["sessions"] == before["sessions"]
    with e.db.read() as c:
        assert not c.execute(
            "SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)
        ).fetchone()
        assert not c.execute(
            "SELECT result_event_id FROM npc_outreach_jobs WHERE id=? AND result_event_id IS NOT NULL",
            (job["id"],),
        ).fetchone()


@pytest.mark.parametrize("damage", ["missing_knowledge", "rumor", "future_knowledge", "heard_only"])
@pytest.mark.parametrize("phase", ["before", "during"])
def test_source_knowledge_is_rechecked_before_and_after_model(town, damage, phase):
    e = town
    job = tick(e)
    binding = current(e)

    def corrupt():
        with e.db.write() as c:
            damage_source(c, e, binding, damage)

    if phase == "before":
        corrupt()
    model = ContactModel(callback=corrupt if phase == "during" else None)
    assert run(e, model) == 0
    assert (model.context is not None) == (phase == "during")
    assert job_status(e, job) == "cancelled"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM player_notifications WHERE id=?", (job["id"],)).fetchone()
        assert not c.execute("SELECT 1 FROM companion_sessions WHERE status='active'").fetchone()


def test_nonwait_activity_and_paused_contact_are_not_relaxed_by_encounter(town):
    e = town
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        LifeActivityService.start(c, actor, e.at, "rest", 10)
    job = tick(e)
    model = ContactModel()
    assert run(e, model) == 0 and model.context is None
    assert job_status(e, job) == "pending"
    assert relationship_choice(e, "boundary", pause_contact=True).status_code == 200
    assert run(e, ContactModel()) == 0 and job_status(e, job) == "cancelled"


def test_paused_contact_does_not_stop_recording_real_scene_changes(town):
    e = town
    assert relationship_choice(e, "boundary", pause_contact=True).status_code == 200
    assert tick(e) is None
    binding = current(e)
    assert binding and not binding["offered_job_id"]
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+0.001 WHERE id=?", (e.npc,))
    tick(e, e.at + timedelta(seconds=1))
    assert current(e) is None


@pytest.mark.parametrize("higher", ["companion", "travel", "continuity"])
def test_real_encounter_does_not_replace_higher_priority_opportunity(town, higher):
    e = town
    at = e.at
    if higher == "companion":
        invitation = companion_choice(e, minutes=30)
    elif higher == "travel":
        journey(e)
    else:
        remember(e)
        at += timedelta(days=1, seconds=1)
    job = tick(e, at)
    binding = current(e)
    assert binding and binding["offered_job_id"] is None
    assert not job["reason"].startswith(EncounterService.JOB_PREFIX)
    if higher == "companion":
        assert job["source_event_id"] == invitation["event_id"]
    elif higher == "travel":
        assert job["reason"].startswith(TravelInteractionService.JOB_PREFIX)
    else:
        with e.db.read() as c:
            assert c.execute(
                "SELECT 1 FROM relationship_continuity_followups WHERE job_id=?", (job["id"],)
            ).fetchone()


@pytest.mark.parametrize("higher", ["companion", "travel", "continuity"])
def test_unclaimed_encounter_yields_to_later_real_priority_without_extra_daily_job(town, higher):
    e = town
    original = tick(e)
    binding = current(e)
    if higher == "companion":
        invitation = companion_choice(e, minutes=30)
    elif higher == "travel":
        journey(e)
    else:
        remember(e, at=e.at - timedelta(days=1, seconds=1))
    updated = tick(e)
    assert updated["id"] == original["id"]
    assert not updated["reason"].startswith(EncounterService.JOB_PREFIX)
    with e.db.read() as c:
        episode = c.execute("SELECT * FROM encounter_episodes WHERE id=?",
                            (binding["episode_id"],)).fetchone()
        assert episode["offered_at"] and episode["offered_job_id"] is None
        assert not EncounterService.opportunity(c, e.wid, e.npc, e.player, e.at)
        assert c.execute("SELECT COUNT(*) FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?",
                         (e.npc, e.player)).fetchone()[0] == 1
        if higher == "companion":
            assert updated["source_event_id"] == invitation["event_id"]
        elif higher == "travel":
            assert updated["reason"].startswith(TravelInteractionService.JOB_PREFIX)
        else:
            assert c.execute("SELECT 1 FROM relationship_continuity_followups WHERE job_id=?",
                             (updated["id"],)).fetchone()
    assert run(e, ContactModel(False)) == 0
    assert job_status(e, updated) == "declined"


def test_existing_paid_budget_is_still_global_four_deliveries(town):
    e = town
    job = tick(e)
    with e.db.write() as c:
        # 哥哥，种子只有三位 NPC；额外两位只属于本项隔离额度夹具。
        for index in range(2):
            c.execute(
                "INSERT INTO characters(id,world_id,name,location_id,energy,satiety,money,"
                "longitude,latitude,created_at,updated_at) "
                "SELECT ?,world_id,?,location_id,energy,satiety,money,longitude,latitude,created_at,updated_at "
                "FROM characters WHERE id=?",
                (str(uuid4()), f"隔离额度人物{index}", e.npc),
            )
        others = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND is_player=0 AND id!=? LIMIT 4",
            (e.wid, e.npc),
        ).fetchall()
        assert len(others) == 4
        for other in others:
            c.execute(
                "INSERT INTO npc_outreach_jobs(id,world_id,npc_id,player_id,channel,reason,world_day,status) "
                "VALUES (?,?,?,?,'in_person','当天已经实际投递过的隔离额度夹具',?,'done')",
                (str(uuid4()), e.wid, other[0], e.player, e.at.date().isoformat()),
            )
    model = ContactModel()
    assert run(e, model) == 0 and model.context is None
    assert job_status(e, job) == "pending"


def test_restart_retains_considered_scene_and_never_turns_it_into_another_first_meeting(town):
    e = town
    job = tick(e)
    binding = current(e)
    assert run(e, ContactModel(False)) == 0
    Database(e.settings.database_path).initialize()
    tick(e, e.at + timedelta(minutes=1))
    restored = current(e)
    assert restored["episode_id"] == binding["episode_id"]
    assert restored["offered_job_id"] == job["id"]
    assert job_status(e, job) == "declined"
