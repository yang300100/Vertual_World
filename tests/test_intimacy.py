"""哥哥，在隔离世界验证双方意愿、主观感受、拒绝和持久相处。"""

# ruff: noqa: F811
from datetime import timedelta
import json
from types import SimpleNamespace

from pydantic import TypeAdapter
from test_lived_world import town  # noqa: F401

from world_engine.conversation.service import ConversationService
from world_engine.decisions import NpcReply
from world_engine.engine import WorldEngine
from world_engine.intimacy import IntimacyService, RelationshipIntent, RelationshipReflection
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import process_outreach
from world_engine.repository import WorldRepository, to_iso
from world_engine.roleplay import salvage_npc_reply
from world_engine.society import SocietyService


def choose(e, operation="propose", **kwargs):
    return e.client.post(f"/api/worlds/{e.wid}/relationships", json={
        "recipient_id": e.npc, "choice": {"operation": operation, "statement": "我希望慢慢了解你，也尊重你的决定。", **kwargs},
    })


def npc_choice(e, operation, **kwargs):
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        return settle_npc_reply(c, npc=npc, player_id=e.player, at=e.at, channel="in_person",
                               reply=NpcReply(reply="我愿意认真对待这份心意。", relationship_intent=RelationshipIntent(
                                   operation=operation, statement="我愿意慢慢了解彼此。", **kwargs)))


def test_separate_consent_and_persistence(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    assert proposal["status"] == "pending"
    assert choose(e, "accept", bond_id=proposal["bond_id"]).status_code == 409
    settled = npc_choice(e, "accept", bond_id=proposal["bond_id"])
    assert settled.action_status == "active"
    restored = e.client.get(f"/api/worlds/{e.wid}/relationships").json()[0]
    assert restored["status"] == "active" and restored["source_event_id"] == settled.action_event_id
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM character_memories WHERE event_id=?", (settled.action_event_id,)).fetchone()[0] == 2
        assert c.execute("SELECT payload_json FROM world_events WHERE id=?", (settled.action_event_id,)).fetchone()


def test_npc_proposal_never_assumes_player_consent(town):
    e = town
    result = npc_choice(e, "propose", kind="close_friend")
    assert result.action_status == "pending"
    bond = e.client.get(f"/api/worlds/{e.wid}/relationships").json()[0]
    assert npc_choice(e, "accept", bond_id=bond["id"]).action_status == "rejected"
    assert choose(e, "accept", bond_id=bond["id"]).json()["status"] == "active"


def test_refusal_cooldown_and_unilateral_exit(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    assert npc_choice(e, "decline", bond_id=proposal["bond_id"]).action_status == "declined"
    assert choose(e, kind="close_friend").status_code == 409
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(e.at + timedelta(days=2)), e.wid))
    e.at += timedelta(days=2)
    proposal = choose(e, kind="close_friend").json()
    assert npc_choice(e, "accept", bond_id=proposal["bond_id"]).action_status == "active"
    assert choose(e, "end", bond_id=proposal["bond_id"]).json()["status"] == "ended"


def test_pause_blocks_proposals_and_queued_outreach(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    npc_choice(e, "accept", bond_id=proposal["bond_id"])
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        c.execute("DELETE FROM npc_outreach_jobs WHERE npc_id!=?", (e.npc,))
    assert choose(e, "boundary", pause_contact=True).status_code == 200
    with e.db.read() as c:
        assert IntimacyService.paused(c, e.wid, e.player, e.npc)
    assert choose(e, kind="romantic").status_code == 409
    engine = WorldEngine(e.db, e.settings, e.model)
    assert process_outreach(engine, e.wid) == 0
    with e.db.read() as c:
        assert c.execute("SELECT status FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?", (e.npc, e.player)).fetchone()[0] == "cancelled"
    choose(e, "boundary", pause_contact=False)
    with e.db.read() as c:
        assert not IntimacyService.paused(c, e.wid, e.player, e.npc)


def test_confirmed_bond_enables_remote_care_without_forcing_contact(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    npc_choice(e, "accept", bond_id=proposal["bond_id"])
    with e.db.write() as c:
        c.execute("INSERT INTO character_contacts(id,world_id,requester_id,recipient_id,status,response_reason,created_at,updated_at) VALUES ('bond-contact',?,?,?,'accepted','好',?,?)",
                  (e.wid, e.player, e.npc, to_iso(e.at), to_iso(e.at)))
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.npc,))
        SocietyService.tick(c, e.wid, e.at)
        job = c.execute("SELECT * FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?", (e.npc, e.player)).fetchone()
        assert job["channel"] == "letter" and "亲密关系" in job["reason"]


def test_expiry_cross_pair_and_no_minted_permissions(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    with e.db.write() as c:
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(e.at + timedelta(days=8)), e.wid))
    assert choose(e, "accept", bond_id=proposal["bond_id"]).status_code == 409
    assert e.client.get(f"/api/worlds/{e.wid}/relationships").json()[0]["status"] == "expired"
    assert choose(e, "accept", bond_id="other-pair-id").status_code == 409
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM character_contacts WHERE requester_id=?", (e.player,)).fetchone()


def test_romance_requires_species_adulthood_and_no_proximity_telepathy(town):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET birth_world_time=? WHERE id=?", (to_iso(e.at - timedelta(days=365)), e.npc))
    assert choose(e, kind="romantic").status_code == 409
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.npc,))
    assert choose(e, kind="close_friend").status_code == 409


def test_private_perspective_survives_without_leaking_to_player(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        reflection = RelationshipReflection(feeling="愿意信任，但还有顾虑", need="慢慢来", concern="私密顾虑标记")
        IntimacyService.reflect(c, npc, e.player, e.at, proposal["event_id"], reflection)
        IntimacyService.reflect(c, npc, e.player, e.at, proposal["event_id"], reflection)
        assert c.execute("SELECT COUNT(*) FROM intimate_perspectives").fetchone()[0] == 1
        ctx = IntimacyService.context(c, e.wid, e.npc, e.player, e.at)
        assert ctx["own_perspective"][0]["concern"] == "私密顾虑标记"
        assert not IntimacyService.context(c, e.wid, e.player, e.npc, e.at)["own_perspective"]
    response = e.client.get(f"/api/worlds/{e.wid}/player/experience")
    assert response.status_code == 200, response.text
    living = response.json()
    assert "私密顾虑标记" not in json.dumps(living, ensure_ascii=False)


def test_context_keeps_confirmed_bond_and_model_json_choices(town):
    e = town
    proposal = choose(e, kind="close_friend").json()
    npc_choice(e, "accept", bond_id=proposal["bond_id"])
    with e.db.read() as c:
        snapshot = WorldRepository().get_snapshot(c, e.wid)
        context = ConversationService().build_npc_reply_context(
            c, snapshot=snapshot, npc=snapshot.character_by_id(e.npc), player=snapshot.character_by_id(e.player),
            player_text="你今天怎么样？", conversation=None,
        )
        assert context["intimacy"]["bonds"][0]["status"] == "active"
    raw = json.dumps({"reply": "我想和你成为挚友。", "relationship_intent": {"operation": "propose", "kind": "close_friend", "statement": "我想和你成为挚友。"}}, ensure_ascii=False)
    result = salvage_npc_reply(raw, TypeAdapter(NpcReply))
    assert result.relationship_intent.kind == "close_friend"


def add_contact(e):
    with e.db.write() as c:
        c.execute("INSERT INTO character_contacts(id,world_id,requester_id,recipient_id,status,created_at,updated_at) VALUES ('letters',?,?,?,'accepted',?,?)",
                  (e.wid, e.player, e.npc, to_iso(e.at), to_iso(e.at)))


def test_letter_relationship_and_private_feeling_commit_together(town):
    e = town
    add_contact(e)
    proposal = choose(e, kind="close_friend").json()
    def complete(*, schema, **kwargs):
        return SimpleNamespace(data=schema.validate_python({
            "reply": "我愿意成为你的挚友，也希望我们慢慢了解彼此。",
            "relationship_intent": {"operation": "accept", "bond_id": proposal["bond_id"], "statement": "我愿意认真维持这份友谊。"},
            "relationship_reflection": {"feeling": "愿意靠近", "need": "慢慢来", "concern": "私密信笺顾虑"},
        }))
    e.model.complete = complete
    response = e.client.post(f"/api/worlds/{e.wid}/messages", json={"recipient_id": e.npc, "content": "我希望听听你对这份心意的想法。"})
    assert response.status_code == 200, response.text
    assert "私密信笺顾虑" not in response.text
    with e.db.read() as c:
        assert c.execute("SELECT status FROM intimate_bonds WHERE id=?", (proposal["bond_id"],)).fetchone()[0] == "active"
        assert c.execute("SELECT concern FROM intimate_perspectives WHERE character_id=?", (e.npc,)).fetchone()[0] == "私密信笺顾虑"


def test_stale_letter_cannot_apply_relationship_choice(town):
    e = town
    add_contact(e)
    proposal = choose(e, kind="close_friend").json()
    def complete(*, schema, **kwargs):
        with e.db.write() as c:
            WorldRepository().bump_version(c, e.wid)
        return SimpleNamespace(data=schema.validate_python({"reply": "我愿意成为挚友。", "relationship_intent": {
            "operation": "accept", "bond_id": proposal["bond_id"], "statement": "我也愿意。"}}))
    e.model.complete = complete
    response = e.client.post(f"/api/worlds/{e.wid}/messages", json={"recipient_id": e.npc, "content": "你愿意吗？"})
    assert response.status_code == 409, response.text
    with e.db.read() as c:
        assert c.execute("SELECT status FROM intimate_bonds WHERE id=?", (proposal["bond_id"],)).fetchone()[0] == "pending"
        assert not c.execute("SELECT 1 FROM character_messages WHERE contact_id='letters'").fetchone()
