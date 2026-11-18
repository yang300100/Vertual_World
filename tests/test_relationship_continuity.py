"""哥哥，验证跨日的本人记忆、真实修复、拒绝联系与私密隔离。"""

# ruff: noqa: F811
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_appointments import people, set_time
from test_lived_world import town  # noqa: F401

from world_engine.actions import ActionService
from world_engine.database import Database
from world_engine.engine import WorldEngine
from world_engine.intimacy import IntimacyService, RelationshipIntent, RelationshipReflection
from world_engine.outreach import process_outreach
from world_engine.relationship_continuity import ContinuityUpdate
from world_engine.relationship_continuity import RelationshipContinuityService as Continuity
from world_engine.repository import from_iso, to_iso
from world_engine.roleplay import npc_reply_system_prompt, salvage_npc_reply
from world_engine.roleplay_pipeline import build_npc_prompt
from world_engine.shared_life import SharedLifeIntent, SharedLifeService
from world_engine.society import SocietyService


def event(e, c, *, at=None, kind="action.socialize", payload=None, other=None):
    return ActionService._record_event(c, world_id=e.wid, tick_id=str(uuid4()), occurred_at=at or e.at,
        event_type=kind, actor_id=e.player, target_id=other or e.npc, location_id=e.loc["id"],
        summary="双方进行了一次真实交流，说明本人想怎样相处。", payload=payload or {"dialogue": "我愿意尊重你的想法。"})


def remember(e, *, kind="concern", topic="约定的可靠性", meaning="私密持续标记：需要看到下次真正守约。", at=None):
    at = at or e.at
    with e.db.write() as c:
        _, npc = people(e, c)
        source = event(e, c, at=at)
        reflection = RelationshipReflection(feeling="愿意慢慢了解", continuity=[ContinuityUpdate(kind=kind, topic=topic, meaning=meaning)])
        IntimacyService.reflect(c, npc, e.player, at, source, reflection)
        row = c.execute("SELECT * FROM relationship_considerations WHERE owner_id=? AND topic=? ORDER BY rowid DESC LIMIT 1", (e.npc, topic)).fetchone()
        return dict(row) if row else None


def update(e, tid, operation="resolve", anchor=None, *, at=None, meaning="根据实际经历重新判断。"):
    at = at or e.at
    with e.db.write() as c:
        _, npc = people(e, c)
        source = event(e, c, at=at)
        Continuity.apply(c, npc, e.player, at, source, [ContinuityUpdate(operation=operation, thread_id=tid, meaning=meaning, anchor_event_id=anchor)])
        return dict(c.execute("SELECT * FROM relationship_considerations WHERE id=?", (tid,)).fetchone())


def completed_time(e):
    with e.db.write() as c:
        player, npc = people(e, c)
        at = from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (e.wid,)).fetchone()[0])
        invite = SharedLifeService.apply(c, npc, player, at, SharedLifeIntent(operation="invite", kind="talk", minutes=15, statement="我们留十五分钟好好说说话，你愿意吗？"))
        SharedLifeService.apply(c, player, npc, at, SharedLifeIntent(operation="accept", moment_id=invite["moment_id"], statement="我愿意留出这十五分钟。"))
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    with e.db.read() as c:
        row = c.execute("SELECT * FROM shared_life_moments WHERE id=?", (invite["moment_id"],)).fetchone()
        assert row["status"] == "completed"
        return row["source_event_id"]


def test_persistent_preference_and_concern_survive_ordinary_reflections_and_restart(town):
    e = town
    concern = remember(e)
    preference = remember(e, kind="preference", topic="相处节奏", meaning="本人喜欢事先约好、每次十五分钟的短聊。")
    with e.db.write() as c:
        _, npc = people(e, c)
        for day in range(1, 15):
            at = e.at + timedelta(days=day)
            source = event(e, c, at=at)
            IntimacyService.reflect(c, npc, e.player, at, source, RelationshipReflection(feeling=f"第{day}天照常过日子"))
    Database(e.settings.database_path).initialize()
    with e.db.read() as c:
        ctx = IntimacyService.context(c,e.wid,e.npc,e.player,e.at+timedelta(days=20))
        assert {concern["id"], preference["id"]} <= {r["id"] for r in ctx["ongoing_considerations"]}
        assert "私密持续标记" not in json.dumps(ctx["own_perspective"], ensure_ascii=False)
        assert all(r["status"] == "active" for r in ctx["ongoing_considerations"])


def test_new_considerations_are_not_blocked_by_six_hour_feeling_deduplication(town):
    e = town
    remember(e)
    remember(e, kind="preference", topic="短时聊天", at=e.at+timedelta(hours=1))
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM intimate_perspectives").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM relationship_considerations").fetchone()[0] == 2


def test_persistent_private_thoughts_do_not_change_scores_or_appear_in_player_interfaces(town):
    e = town
    with e.db.write() as c:
        _,npc=people(e,c)
        source=event(e,c)
        before = [tuple(r) for r in c.execute("SELECT * FROM relationships ORDER BY rowid")]
        Continuity.apply(c,npc,e.player,e.at,source,[ContinuityUpdate(kind="concern",topic="可靠性",meaning="私密持续标记：需要看到真实守约。")])
    with e.db.read() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM relationships ORDER BY rowid")] == before
        assert not IntimacyService.context(c,e.wid,e.player,e.npc,e.at)["ongoing_considerations"]
        assert "私密持续标记" not in " ".join(r[0] for r in c.execute("SELECT summary FROM world_events"))
    for url in (f"/api/worlds/{e.wid}/player/experience", f"/api/worlds/{e.wid}/relationships/{e.npc}/memories"):
        response = e.client.get(url)
        assert response.status_code == 200
        assert "私密持续标记" not in response.text


@pytest.mark.parametrize("bad", ["missing", "hidden", "unknown", "other_pair", "future", "future_knowledge"])
def test_invented_hidden_unobserved_and_other_peoples_evidence_is_rejected(town, bad):
    e = town
    with e.db.write() as c:
        _, npc = people(e, c)
        source = event(e, c)
        third = c.execute("SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1", (e.wid,e.player,e.npc)).fetchone()[0]
        anchor = "missing" if bad == "missing" else event(e,c,at=e.at+timedelta(days=1) if bad=="future" else e.at,
            payload={"information_scope":"author_hidden"} if bad=="hidden" else None,other=third if bad=="other_pair" else None)
        if bad == "unknown":
            c.execute("DELETE FROM character_event_knowledge WHERE character_id=? AND event_id=?",(e.npc,anchor))
        if bad == "hidden":
            c.execute("UPDATE world_events SET payload_json=json_set(payload_json,'$.information_scope','author_hidden') WHERE id=?",(anchor,))
        if bad == "future_knowledge":
            c.execute("UPDATE character_event_knowledge SET learned_world_time=? WHERE event_id=? AND character_id=?",(to_iso(e.at+timedelta(days=1)),anchor,e.npc))
        Continuity.apply(c,npc,e.player,e.at,source,[ContinuityUpdate(kind="concern",topic="不可知来源",meaning="不应被记录",anchor_event_id=anchor)])
        assert not c.execute("SELECT 1 FROM relationship_considerations").fetchone()
        assert c.execute("SELECT applied FROM relationship_consideration_updates").fetchone()[0] == 0


def test_player_cannot_be_assigned_private_npc_attitudes(town):
    e = town
    with e.db.write() as c:
        player, _ = people(e,c)
        source=event(e,c)
        Continuity.apply(c,player,e.npc,e.at,source,[ContinuityUpdate(kind="hope",topic="替玩家决定",meaning="不应被记录")])
        assert not c.execute("SELECT 1 FROM relationship_considerations").fetchone()


@pytest.mark.parametrize("fake_completed", [False,True])
def test_apology_or_forged_completion_cannot_clear_existing_concern(town, fake_completed):
    e = town
    thread=remember(e)
    with e.db.write() as c:
        anchor=event(e,c,at=e.at+timedelta(minutes=1),kind="social.shared_completed" if fake_completed else "action.socialize",payload={"dialogue":"对不起，我以后一定来。","moment_id":"invented"})
    after=update(e,thread["id"],anchor=anchor,at=e.at+timedelta(minutes=2))
    assert after["status"] == "active" and after["version"] == 1
    with e.db.read() as c:
        assert "真正兑现" in c.execute("SELECT reason FROM relationship_consideration_updates ORDER BY rowid DESC LIMIT 1").fetchone()[0]


def test_real_later_completion_can_support_npcs_own_reassessment_and_remains_known(town):
    e = town
    thread=remember(e)
    completion=completed_time(e)
    after=update(e,thread["id"],anchor=completion,at=e.at+timedelta(minutes=16),meaning="这次确实一起度过了约定时间，我愿意放下这项具体顾虑。")
    assert after["status"] == "resolved"
    with e.db.read() as c:
        ctx=Continuity.context(c,e.wid,e.npc,e.player,e.at+timedelta(days=60))
        assert ctx[0]["status"] == "resolved" and ctx[0]["anchor_event_id"] == completion


def test_previous_completion_is_not_fresh_evidence_for_new_concern(town):
    e=town
    old=completed_time(e)
    thread=remember(e,at=e.at+timedelta(minutes=16))
    assert update(e,thread["id"],anchor=old,at=e.at+timedelta(minutes=17))["status"] == "active"


def test_shelving_is_a_personal_choice_and_not_forged_repair(town):
    e=town
    thread=remember(e)
    result=update(e,thread["id"],operation="set_aside",at=e.at+timedelta(minutes=1),meaning="我不想反复追问此事，先放在一边；这并不表示已经修复。")
    assert result["status"] == "set_aside"
    with e.db.read() as c:
        assert Continuity.context(c,e.wid,e.npc,e.player,e.at+timedelta(days=2))[0]["status"] == "set_aside"
        assert Continuity.opportunity(c,e.wid,e.npc,e.player,e.at+timedelta(days=2)) is None


def test_duplicate_memory_and_unchanged_revision_do_not_refresh_followup_clock(town):
    e=town
    original=remember(e)
    duplicate=remember(e,at=e.at+timedelta(hours=12))
    assert duplicate["id"] == original["id"] and duplicate["updated_at"] == original["updated_at"]
    same=update(e,original["id"],operation="revise",at=e.at+timedelta(hours=14),meaning=original["meaning"])
    assert same["version"] == 1 and same["updated_at"] == original["updated_at"]
    revised=update(e,original["id"],operation="revise",at=e.at+timedelta(hours=15),meaning="我愿意尝试一次更短的安排，但仍需要实际兑现。")
    assert revised["status"] == "active" and revised["version"] == 2
    with e.db.read() as c:
        assert Continuity.opportunity(c,e.wid,e.npc,e.player,e.at+timedelta(days=1)) is None
        assert Continuity.opportunity(c,e.wid,e.npc,e.player,e.at+timedelta(days=2))["id"] == original["id"]


def test_repeated_same_model_result_is_idempotent_and_active_storage_is_bounded(town):
    e=town
    with e.db.write() as c:
        _,npc=people(e,c)
        source=event(e,c)
        proposal=ContinuityUpdate(kind="preference",topic="短时",meaning="我更适应短些的交流。")
        for _ in range(2):
            Continuity.apply(c,npc,e.player,e.at,source,[proposal])
        assert c.execute("SELECT COUNT(*) FROM relationship_consideration_updates").fetchone()[0] == 1
    for i in range(8):
        remember(e,topic=f"独立事项{i}")
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM relationship_considerations WHERE status='active'").fetchone()[0] == 8


class ContactModel:
    def __init__(self, should_contact):
        self.should_contact=should_contact
        self.context=None

    def complete(self, *, schema, user_payload, **kwargs):
        self.context=user_payload
        return SimpleNamespace(data=schema.validate_python({"should_contact":self.should_contact,"reply":"我想继续和你聊聊，看看彼此现在愿意怎样相处。" if self.should_contact else None}))


def queue(e,at):
    set_time(e,at)
    with e.db.write() as c:
        SocietyService.tick(c,e.wid,at)
        return dict(c.execute("SELECT j.* FROM npc_outreach_jobs j JOIN relationship_continuity_followups f ON f.job_id=j.id WHERE j.npc_id=? ORDER BY j.rowid DESC LIMIT 1",(e.npc,)).fetchone())


@pytest.mark.parametrize("should_contact",[False,True])
def test_cross_day_followup_is_optional_private_and_considered_once_per_version(town,should_contact):
    e=town
    remember(e)
    with e.db.read() as c:
        assert Continuity.opportunity(c,e.wid,e.npc,e.player,e.at+timedelta(hours=23)) is None
    job=queue(e,e.at+timedelta(days=1))
    assert "私密持续标记" not in job["reason"]
    engine=WorldEngine(e.db,e.settings,e.model)
    model=ContactModel(should_contact)
    engine.agent_model_backend=model
    assert process_outreach(engine,e.wid) == int(should_contact)
    assert "私密持续标记" in json.dumps(model.context["intimacy"]["ongoing_considerations"],ensure_ascii=False)
    with e.db.read() as c:
        state=c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?",(job["id"],)).fetchone()[0]
        assert state == ("done" if should_contact else "declined")
        assert Continuity.opportunity(c,e.wid,e.npc,e.player,e.at+timedelta(days=3)) is None
        assert "私密持续标记" not in " ".join(r[0] for r in c.execute("SELECT summary FROM world_events"))


def test_followup_cancels_if_private_matters_changed_before_model_call(town):
    e=town
    thread=remember(e)
    job=queue(e,e.at+timedelta(days=1))
    update(e,thread["id"],operation="set_aside",at=e.at+timedelta(days=1,minutes=1))
    engine=WorldEngine(e.db,e.settings,e.model)
    engine.agent_model_backend=ContactModel(True)
    assert process_outreach(engine,e.wid) == 0 and engine.agent_model_backend.context is None
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?",(job["id"],)).fetchone()[0] == "cancelled"


def test_unclaimed_generic_greeting_can_pick_up_a_later_due_matter(town):
    e=town
    with e.db.write() as c:
        player,npc=people(e,c)
        proposal=IntimacyService.apply(c,npc,player,e.at,RelationshipIntent(operation="propose",kind="close_friend",statement="我们愿意作为挚友相处。"))
        IntimacyService.apply(c,player,npc,e.at,RelationshipIntent(operation="accept",bond_id=proposal["bond_id"],statement="我也明确愿意。"))
    thread=remember(e,at=e.at+timedelta(hours=5))
    at=e.at+timedelta(days=1)
    set_time(e,at)
    with e.db.write() as c:
        SocietyService.tick(c,e.wid,at)
        generic=c.execute("SELECT id FROM npc_outreach_jobs WHERE npc_id=? AND world_day=?",(e.npc,at.date().isoformat())).fetchone()[0]
        assert not c.execute("SELECT 1 FROM relationship_continuity_followups WHERE job_id=?",(generic,)).fetchone()
        SocietyService.tick(c,e.wid,at+timedelta(hours=5))
        assert c.execute("SELECT thread_id FROM relationship_continuity_followups WHERE job_id=?",(generic,)).fetchone()[0] == thread["id"]


def test_other_npc_cannot_edit_an_owners_private_matter(town):
    e=town
    thread=remember(e)
    with e.db.write() as c:
        third=c.execute("SELECT * FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",(e.wid,e.player,e.npc)).fetchone()
        source=event(e,c,other=third["id"])
        Continuity.apply(c,third,e.player,e.at,source,[ContinuityUpdate(operation="set_aside",thread_id=thread["id"],meaning="不允许替别人搁置")])
        assert c.execute("SELECT status FROM relationship_considerations WHERE id=?",(thread["id"],)).fetchone()[0] == "active"


def test_explicit_pause_contact_blocks_queued_persistent_followup(town):
    e=town
    remember(e)
    job=queue(e,e.at+timedelta(days=1))
    with e.db.write() as c:
        player,npc=people(e,c)
        IntimacyService.apply(c,player,npc,e.at+timedelta(days=1),RelationshipIntent(operation="boundary",pause_contact=True,statement="我需要空间，暂时不要主动联系我。"))
    engine=WorldEngine(e.db,e.settings,e.model)
    engine.agent_model_backend=ContactModel(True)
    assert process_outreach(engine,e.wid) == 0
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?",(job["id"],)).fetchone()[0] == "cancelled"


def test_focused_hope_is_not_hidden_behind_other_private_concerns(town):
    e=town
    hope=remember(e,kind="hope",topic="再聊一次",meaning="希望以后再留出十五分钟交谈。")
    for i in range(4):
        remember(e,topic=f"另外的顾虑{i}")
    job=queue(e,e.at+timedelta(days=1))
    engine=WorldEngine(e.db,e.settings,e.model)
    engine.agent_model_backend=ContactModel(False)
    process_outreach(engine,e.wid)
    with e.db.read() as c:
        focus=c.execute("SELECT thread_id FROM relationship_continuity_followups WHERE job_id=?",(job["id"],)).fetchone()[0]
    assert focus == hope["id"]
    assert engine.agent_model_backend.context["intimacy"]["ongoing_considerations"][0]["id"] == hope["id"]


def test_context_stays_within_real_roleplay_prompt_budget(town):
    e=town
    for i in range(8):
        remember(e,topic=f"有限的持续事项{i}",meaning="我愿意慢慢了解，但希望每次先商量清楚，再看实际行动。"*3)
    engine=WorldEngine(e.db,e.settings,e.model)
    with e.db.read() as c:
        snapshot=engine.repository.get_snapshot(c,e.wid)
        context=engine.dialogue_context.build(c,snapshot=snapshot,npc=snapshot.character_by_id(e.npc),player=snapshot.character_by_id(e.player),player_text="你还记得之前在意的事情吗？",conversation=None,channel="in_person",interaction="继续相处")
        prompt=build_npc_prompt(npc_reply_system_prompt(),context)
        assert prompt.input_tokens <= 8192 and len(context["intimacy"]["ongoing_considerations"]) == 3
        tight=build_npc_prompt(npc_reply_system_prompt(),context,max_input_tokens=prompt.trace["mandatory"]["tokens"]+20)
        sent=json.loads(tight.messages[1]["content"])["intimacy"]["ongoing_considerations"]
        assert len(sent)==1 and sent[0]["id"]==context["intimacy"]["ongoing_considerations"][0]["id"]
        assert not tight.trace["intimacy.ongoing_considerations:1"]["included"]


def test_structured_reflection_salvage_keeps_durable_choice_and_null_defaults_are_safe():
    from pydantic import TypeAdapter

    from world_engine.decisions import NpcReply

    raw={"reply":"我更喜欢慢慢来。","relationship_reflection":{"feeling":"愿意继续了解","continuity":[{"kind":"preference","topic":"慢慢来","meaning":"我希望先短聊再考虑更长的见面。"}]}}
    reply=salvage_npc_reply(json.dumps(raw,ensure_ascii=False),TypeAdapter(NpcReply))
    assert reply.relationship_reflection.continuity[0].kind == "preference"
    assert not RelationshipReflection(feeling="照常",continuity=None).continuity
