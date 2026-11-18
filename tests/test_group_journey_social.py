"""哥哥，多人邀请与途中交流都只给本人一次独立考虑，不替全组作答。"""

# ruff: noqa: F811
from types import SimpleNamespace

import pytest
from test_group_journey_text_api import post, proposal, trio  # noqa: F401
from test_lived_world import town  # noqa: F401

from world_engine.engine import WorldEngine
from world_engine.group_journeys import GroupJourneyIntent, GroupJourneyService
from world_engine.outreach import process_outreach
from world_engine.repository import from_iso
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.roleplay_pipeline import build_npc_prompt
from world_engine.society import SocietyService
from world_engine.travel_interaction import TravelInteractionService


def now(e, c):
    return from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])


def choose(e, cid, jid, operation):
    with e.db.write() as c:
        actor = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        return GroupJourneyService.apply(c, actor, now(e, c), GroupJourneyIntent(
            operation=operation, journey_id=jid, statement="这是我本人对原多人条款的明确选择。"))


class IndependentModel:
    def __init__(self, *, contact, accept=None):
        self.contact, self.accept, self.contexts = contact, accept, []

    def complete(self, *, schema, user_payload, **kwargs):
        self.contexts.append(user_payload)
        raw = {"should_contact": self.contact, "reply": "我表达本人的选择。" if self.contact else None}
        if self.contact and self.accept:
            raw["group_journey_intent"] = {"operation": "accept", "journey_id": self.accept,
                                          "statement": "我本人愿意参加原完整有限多人行程。"}
        return SimpleNamespace(data=schema.validate_python(raw))


def engine(e, model):
    result = WorldEngine(e.db, e.settings, e.model)
    result.agent_model_backend = model
    return result


def started(e, *, policy="pause"):
    fields = proposal(e)
    fields["separation_policy"] = policy
    response = post(e, fields)
    assert response.status_code == 200, response.text
    jid = response.json()["journey_id"]
    for cid in (e.npc, e.third):
        choose(e, cid, jid, "accept")
    choose(e, e.player, jid, "follow")
    e.clock.heartbeat(e.wid, elapsed_seconds=1)
    return jid


@pytest.mark.parametrize("contact", [False, True])
def test_society_gives_each_invited_npc_own_source_and_independent_choice(trio, contact):
    e = trio
    response = post(e, proposal(e))
    assert response.status_code == 200, response.text
    jid = response.json()["journey_id"]
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, now(e, c))
        row = c.execute("SELECT * FROM group_journeys WHERE id=?", (jid,)).fetchone()
        jobs = c.execute("SELECT * FROM npc_outreach_jobs WHERE npc_id IN (?,?)", (e.npc, e.third)).fetchall()
        assert len(jobs) == 2
        assert all(job["source_event_id"] == row["proposal_event_id"] for job in jobs)
        assert c.execute("SELECT COUNT(*) FROM group_journey_members WHERE decision='accepted'").fetchone()[0] == 1
    model = IndependentModel(contact=contact, accept=jid)
    runner = engine(e, model)
    try:
        assert process_outreach(runner, e.wid) == int(contact)
        with e.db.read() as c:
            assert c.execute("SELECT status FROM group_journeys WHERE id=?", (jid,)).fetchone()[0] == "pending"
            assert c.execute("SELECT COUNT(*) FROM group_journey_members WHERE decision='accepted'").fetchone()[0] == (2 if contact else 1)
            assert not c.execute("SELECT 1 FROM character_movements WHERE status='moving'").fetchone()
        assert process_outreach(runner, e.wid) == int(contact)
        with e.db.read() as c:
            assert c.execute("SELECT status FROM group_journeys WHERE id=?", (jid,)).fetchone()[0] == ("accepted" if contact else "pending")
            assert c.execute("SELECT follow_source_event_id FROM group_journeys WHERE id=?", (jid,)).fetchone()[0] is None
            assert not c.execute("SELECT 1 FROM character_movements WHERE status='moving'").fetchone()
        assert len(model.contexts) == 2
        assert all(context["decision"]["may_decline"] for context in model.contexts)
    finally:
        runner.close()


@pytest.mark.parametrize("contact", [False, True])
def test_group_members_may_talk_in_real_current_leg_without_stopping_others(trio, contact):
    e = trio
    started(e)
    with e.db.read() as c:
        at = now(e, c)
        before = [tuple(row) for row in c.execute("SELECT * FROM character_movements ORDER BY id")]
        for cid in (e.npc, e.third):
            binding = TravelInteractionService.shared_motion(c, e.wid, cid, e.player, at)
            assert binding and binding["kind"] == "group_journey"
        jobs = c.execute("SELECT * FROM npc_outreach_jobs WHERE npc_id IN (?,?)", (e.npc, e.third)).fetchall()
        assert len(jobs) == 2 and all(job["reason"].startswith(TravelInteractionService.JOB_PREFIX) for job in jobs)
    model = IndependentModel(contact=contact)
    runner = engine(e, model)
    try:
        assert process_outreach(runner, e.wid) == int(contact)
        assert model.contexts[0]["shared_travel"]["kind"] == "group_journey"
        assert not model.contexts[0]["shared_travel"]["has_arrived"]
        assert "npc_movement_id" not in model.contexts[0]["shared_travel"]
        with e.db.read() as c:
            assert [tuple(row) for row in c.execute("SELECT * FROM character_movements ORDER BY id")] == before
    finally:
        runner.close()


def test_pausing_group_invalidates_old_leg_job_before_model_call(trio):
    e = trio
    jid = started(e)
    choose(e, e.player, jid, "pause")
    model = IndependentModel(contact=True)
    runner = engine(e, model)
    try:
        assert process_outreach(runner, e.wid) == 0
        assert not model.contexts
    finally:
        runner.close()


def test_departed_member_loses_old_chat_while_remaining_group_keeps_current_leg(trio):
    e = trio
    jid = started(e, policy="continue")
    choose(e, e.third, jid, "leave")
    with e.db.read() as c:
        assert TravelInteractionService.shared_motion(c, e.wid, e.third, e.player, now(e, c)) is None
        assert TravelInteractionService.shared_motion(c, e.wid, e.npc, e.player, now(e, c))["kind"] == "group_journey"


def test_actual_group_context_and_whole_current_terms_fit_original_npc_input_gate(trio):
    e = trio
    response = post(e, proposal(e))
    assert response.status_code == 200
    runner = engine(e, IndependentModel(contact=False))
    try:
        with e.db.read() as c:
            snapshot = runner.repository.get_snapshot(c, e.wid)
            context = runner.dialogue_context.build(c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc),
                player=snapshot.character_by_id(e.player), player_text="请你只按自己的想法决定是否参加这次多人旅行。",
                conversation=None, channel="in_person", interaction="本人独立回应完整多人旅行")
        prompt = build_npc_prompt(npc_reply_system_prompt(), context)
        assert prompt.input_tokens <= 8192
        assert response.json()["journey_id"] in str(prompt.messages)
        assert all(cid in str(prompt.messages) for cid in e.ids)
    finally:
        runner.close()
