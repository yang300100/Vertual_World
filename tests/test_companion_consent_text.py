"""哥哥，交谈和表单表达本人意愿，不能替另一人作出同行决定。"""

# ruff: noqa: F811
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from test_lived_world import town  # noqa: F401

from world_engine.api_dialogue import NpcTextResult
from world_engine.companion_proposals import CompanionIntent
from world_engine.decisions import NpcReply
from world_engine.domain import CharacterState
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParseResult, IntentParserAgent
from world_engine.npc_performance import settle_npc_reply
from world_engine.outreach import OutreachText, process_outreach
from world_engine.repository import from_iso
from world_engine.roleplay import salvage_npc_reply
from world_engine.society import SocietyService


def parse(choice, context, *, apart=False, evidence=None):
    player=CharacterState(id="player",world_id="world",name="旅人",location_id="place",is_player=True,
                          energy=90,satiety=100,money=50,current_room_id="mine" if apart else None)
    npc=player.model_copy(update={"id":"npc","name":"对方","is_player":False,"current_room_id":"yours" if apart else None})
    class Backend:
        def complete(self,**request):
            return SimpleNamespace(data=IntentParseResult(operation="companion",target_character_id="npc",
                companion_choice=choice,choice_evidence=evidence if evidence is not None else choice.statement))
    return IntentParserAgent(Backend()).preview(intent=choice.statement,player=player,
        snapshot=SimpleNamespace(characters=[player,npc],world=SimpleNamespace(current_time=datetime(2030,1,1,tzinfo=UTC))),
        preferred_target_id="npc",social_context={"npc":context})


@pytest.mark.parametrize("operation",["accept","decline","counter","cancel"])
@pytest.mark.parametrize("failure",[None,"unknown","wrong_role","expired","forged_evidence"])
def test_parser_requires_pending_shared_proposal_actual_person_and_exact_words(operation,failure):
    fields={"minutes":15} if operation=="counter" else {}
    choice=CompanionIntent(operation=operation,proposal_id="proposal",statement="这是我本人对这份同行提议的明确选择。",**fields)
    own=operation=="cancel"
    row=dict(id="proposal",status="pending",proposer_id="player" if own else "npc",recipient_id="npc" if own else "player")
    if failure=="unknown":row["id"]="other"
    if failure=="wrong_role":row["proposer_id"],row["recipient_id"]=row["recipient_id"],row["proposer_id"]
    if failure=="expired":row["status"]="expired"
    result=parse(choice,{"companion_proposals":[row]},evidence="并非本人原话。" if failure=="forged_evidence" else None)
    assert (result.route_kind=="social_choice") is (failure is None)


@pytest.mark.parametrize("operation",["propose","accept","decline","counter","cancel","end"])
def test_apart_known_companion_only_allows_own_cancel_or_end(operation):
    fields={"minutes":15} if operation=="propose" else {"session_id":"session"} if operation=="end" else {"proposal_id":"proposal",**({"minutes":15} if operation=="counter" else {})}
    choice=CompanionIntent(operation=operation,statement="这是本人对已有同行安排的明确选择。",**fields)
    row=dict(id="proposal",status="pending",proposer_id="player" if operation=="cancel" else "npc",recipient_id="npc" if operation=="cancel" else "player")
    result=parse(choice,{"companion_only":True,"companion_proposals":[row],"companionship":[{"id":"session"}]},apart=True)
    assert (result.route_kind=="social_choice") is (operation in {"cancel","end"})


@pytest.mark.parametrize("schema",[NpcReply,NpcTextResult,OutreachText])
def test_dialogue_letter_outreach_and_salvage_keep_real_companion_choice(schema):
    raw={"reply":"我愿意考虑十五分钟的同行。","companion_intent":{
        "operation":"counter","proposal_id":"actual-proposal","minutes":15,"statement":"我只能同行十五分钟，你愿意吗？"}}
    if schema is OutreachText:raw["should_contact"]=True
    parsed=schema.model_validate(raw)
    assert parsed.companion_intent.operation=="counter" and parsed.companion_intent.minutes==15
    if schema is not OutreachText:
        salvaged=salvage_npc_reply(json.dumps(raw,ensure_ascii=False),TypeAdapter(schema))
        assert salvaged.companion_intent==parsed.companion_intent


def test_api_original_words_pending_replay_and_npc_statement_alone_do_not_activate(town):
    e=town
    text="我明确邀请你现在一起徒步三十分钟，是否愿意由你本人决定。"
    payload=dict(request_id=str(uuid4()),recipient_id=e.npc,intent=text,evidence=text,
                 companion_choice=dict(operation="propose",minutes=30,statement="另一段不应写入的候选文字。"))
    first=e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=payload)
    assert first.status_code==200,first.text
    result=first.json()
    assert result["status"]=="pending"
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=payload).json()==result
    with e.db.write() as c:
        at=from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?",(e.wid,)).fetchone()[0])
        npc=c.execute("SELECT * FROM characters WHERE id=?",(e.npc,)).fetchone()
        settle_npc_reply(c,npc=npc,player_id=e.player,at=at,channel="in_person",reply=NpcReply(reply="我愿意与你同行。"))
        assert not c.execute("SELECT 1 FROM companion_sessions WHERE status='active'").fetchone()
        assert c.execute("SELECT statement FROM companion_proposals WHERE id=?",(result["proposal_id"],)).fetchone()[0]==text
        chosen=CompanionIntent(operation="accept",proposal_id=result["proposal_id"],statement="我本人同意这三十分钟的同行。")
        settled=settle_npc_reply(c,npc=npc,player_id=e.player,at=at,channel="in_person",reply=NpcReply(reply=chosen.statement,companion_intent=chosen))
        assert c.execute("SELECT 1 FROM companion_sessions WHERE status='active' AND proposal_id=?",(result["proposal_id"],)).fetchone()
        assert settled.reply


def test_player_cannot_accept_own_proposal_through_new_or_old_api(town):
    e=town
    original=e.client.post(f"/api/worlds/{e.wid}/companions",json={"recipient_id":e.npc,"minutes":15})
    assert original.status_code==200,original.text
    row=original.json();assert row["status"]=="pending"
    text="我接受这份已有同行提议。"
    result=e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=dict(request_id=str(uuid4()),
        recipient_id=e.npc,intent=text,evidence=text,companion_choice=dict(operation="accept",proposal_id=row["proposal_id"],statement=text)))
    assert result.status_code==409
    assert e.client.get(f"/api/worlds/{e.wid}/companions").json()==[]


@pytest.mark.parametrize("finish_before_considering",[False,True])
def test_pending_invite_offers_independent_attention_but_never_forces_reply_or_stale_call(town,finish_before_considering):
    e=town
    created=e.client.post(f"/api/worlds/{e.wid}/companions",json={"recipient_id":e.npc,"minutes":15})
    assert created.status_code==200,created.text
    proposal=created.json()
    with e.db.write() as c:
        c.execute("DELETE FROM npc_outreach_jobs WHERE world_id=?",(e.wid,))
        c.execute("UPDATE characters SET traits_json=? WHERE id=?",(json.dumps(["谨慎","慢热"]),e.npc))
        c.execute("UPDATE relationships SET trust=-20,affinity=-20 WHERE world_id=? AND source_character_id=? AND target_character_id=?",(e.wid,e.npc,e.player))
        SocietyService.tick(c,e.wid,e.at)
        c.execute("DELETE FROM npc_outreach_jobs WHERE npc_id!=?",(e.npc,))
        job=dict(c.execute("SELECT * FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?",(e.npc,e.player)).fetchone())
        assert job["source_event_id"]==proposal["event_id"] and "同行" in job["reason"]
    if finish_before_considering:
        text="我撤回自己这份待回应的同行提议。"
        cancelled=e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=dict(request_id=str(uuid4()),
            recipient_id=e.npc,intent=text,evidence=text,companion_choice=dict(operation="cancel",proposal_id=proposal["proposal_id"],statement=text)))
        assert cancelled.status_code==200,cancelled.text
    class Backend:
        calls=0
        def complete(self,**request):
            self.calls+=1
            return SimpleNamespace(data=OutreachText(should_contact=False,reply=None))
    backend=Backend()
    engine=WorldEngine(e.db,e.settings,e.model)
    engine.agent_model_backend=backend
    try:
        assert process_outreach(engine,e.wid)==0
        assert backend.calls==(0 if finish_before_considering else 1)
        with e.db.read() as c:
            state=c.execute("SELECT status FROM npc_outreach_jobs WHERE id=?",(job["id"],)).fetchone()[0]
            assert state==("cancelled" if finish_before_considering else "declined")
            assert not c.execute("SELECT 1 FROM companion_sessions WHERE status='active'").fetchone()
            assert c.execute("SELECT status FROM companion_proposals WHERE id=?",(proposal["proposal_id"],)).fetchone()[0]==("cancelled" if finish_before_considering else "pending")
    finally:
        engine.close()
