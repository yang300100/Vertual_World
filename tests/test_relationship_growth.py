"""哥哥，验证自然表达、真实共同生活和不会遗失的重要往事。"""

# ruff: noqa: F811
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_lived_world import town  # noqa: F401

from world_engine.decisions import NpcReply
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent
from world_engine.intimacy import IntimacyService, RelationshipIntent, RelationshipReflection
from world_engine.actions import ActionService
from world_engine.inventory import InventoryService
from world_engine.npc_performance import settle_npc_reply
from world_engine.repository import WorldRepository, to_iso
from world_engine.shared_life import SharedLifeIntent, SharedLifeService


def invite(e, kind="talk", minutes=15):
    response = e.client.post(f"/api/worlds/{e.wid}/shared-life", json={
        "recipient_id": e.npc, "choice": {"operation": "invite", "kind": kind, "minutes": minutes, "statement": "我想和你一起度过一会儿时间，你愿意吗？"},
    })
    assert response.status_code == 200, response.text
    return response.json()


def respond(e, mid, operation="accept"):
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        return settle_npc_reply(c, npc=npc, player_id=e.player, at=e.at, channel="in_person", reply=NpcReply(
            reply="我愿意一起安排这段时间。", shared_life_intent=SharedLifeIntent(operation=operation, moment_id=mid, statement="我愿意一起安排这段时间。")))


def give_player_food(e):
    with e.db.write() as c:
        source = c.execute("SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.owner_character_id=? AND i.item_type_id=?", (e.npc, e.food)).fetchone()
        InventoryService.transfer(c, source, e.player, 2)


def test_meal_uses_real_food_and_time_then_keeps_memory(town):
    e = town
    give_player_food(e)
    proposal = invite(e, "meal", 15)
    with e.db.read() as c:
        before = c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=?", (e.food,)).fetchone()[0]
        assert not c.execute("SELECT 1 FROM character_life_activities WHERE status='running'").fetchone()
    result = respond(e, proposal["moment_id"])
    assert result.action_status == "active", result
    with e.db.read() as c:
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=?", (e.food,)).fetchone()[0] == before-2
        assert c.execute("SELECT COUNT(*) FROM character_life_activities WHERE status='running'").fetchone()[0] == 2
        experiences=c.execute("SELECT shared_experiences FROM character_acquaintances WHERE observer_id=? AND subject_id=?",(e.player,e.npc)).fetchone()[0]
    assert respond(e, proposal["moment_id"]).action_status == "rejected"
    e.clock.heartbeat(e.wid, elapsed_seconds=15*60)
    with e.db.read() as c:
        assert c.execute("SELECT status FROM shared_life_moments WHERE id=?", (proposal["moment_id"],)).fetchone()[0] == "completed"
        assert c.execute("SELECT shared_experiences FROM character_acquaintances WHERE observer_id=? AND subject_id=?",(e.player,e.npc)).fetchone()[0]==experiences+1
        assert any("共同用餐" in row["meaning"] for row in IntimacyService.landmarks(c, e.wid, e.player, e.npc))


def test_missing_food_rolls_back_both_activities_and_inventory(town):
    e = town
    proposal = invite(e, "meal")
    with e.db.read() as c:
        before = c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=?", (e.food,)).fetchone()[0]
    assert respond(e, proposal["moment_id"]).action_status == "rejected"
    with e.db.read() as c:
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE item_type_id=?", (e.food,)).fetchone()[0] == before
        assert not c.execute("SELECT 1 FROM character_life_activities WHERE status='running'").fetchone()
        assert c.execute("SELECT status FROM shared_life_moments").fetchone()[0] == "pending"


def test_mutual_consent_rejection_and_exit(town):
    e = town
    proposal = invite(e)
    response = e.client.post(f"/api/worlds/{e.wid}/shared-life", json={"recipient_id":e.npc,"choice":{"operation":"accept","moment_id":proposal["moment_id"],"statement":"我愿意"}})
    assert response.status_code == 409
    assert respond(e, proposal["moment_id"], "decline").action_status == "declined"
    proposal = invite(e, "rest", 20)
    assert respond(e, proposal["moment_id"]).action_status == "active"
    response = e.client.post(f"/api/worlds/{e.wid}/shared-life", json={"recipient_id":e.npc,"choice":{"operation":"end","moment_id":proposal["moment_id"],"statement":"我想先独处"}})
    assert response.json()["status"] == "interrupted"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM character_life_activities WHERE character_id IN (?,?) AND status='running'", (e.player,e.npc)).fetchone()


def test_body_contact_requires_fresh_consent_and_three_meters(town):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET latitude=latitude+0.00005 WHERE id=?", (e.npc,))
    proposal = invite(e, "hug")
    assert respond(e, proposal["moment_id"]).action_status == "rejected"
    with e.db.write() as c:
        c.execute("UPDATE characters SET latitude=? WHERE id=?", (e.loc["latitude"],e.npc))
    assert respond(e, proposal["moment_id"]).action_status == "active"
    with e.db.read() as c:
        assert c.execute("SELECT minutes FROM shared_life_moments").fetchone()[0] == 1
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    second = invite(e, "hug")
    assert second["status"] == "pending"


def test_separation_interrupts_and_never_teleports(town):
    e = town
    proposal = invite(e, "talk")
    assert respond(e, proposal["moment_id"]).action_status == "active"
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.npc,))
        lon = c.execute("SELECT longitude FROM characters WHERE id=?", (e.npc,)).fetchone()[0]
        SharedLifeService.maintain(c, e.wid, e.at+timedelta(minutes=1))
        assert c.execute("SELECT status FROM shared_life_moments").fetchone()[0] == "interrupted"
        assert c.execute("SELECT longitude FROM characters WHERE id=?", (e.npc,)).fetchone()[0] == lon


def test_important_emotion_is_saved_inside_six_hours_and_private(town):
    e = town
    first = invite(e)
    with e.db.write() as c:
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        IntimacyService.reflect(c,npc,e.player,e.at,first["event_id"],RelationshipReflection(feeling="还在了解对方"))
    declined = respond(e,first["moment_id"],"decline")
    with e.db.write() as c:
        # 哥哥，先把隔离世界推进到感受形成的时刻，再读取这段真实记忆。
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(e.at+timedelta(minutes=1)),e.wid))
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e.npc,)).fetchone()
        IntimacyService.reflect(c,npc,e.player,e.at+timedelta(minutes=1),declined.action_event_id,RelationshipReflection(
            feeling="愿意慢慢信任", significance="milestone", meaning="私密意义标记：对方接受我的拒绝，让我感到被尊重"))
        assert c.execute("SELECT COUNT(*) FROM intimate_perspectives").fetchone()[0] == 2
        assert IntimacyService.landmarks(c,e.wid,e.npc,e.player)
        assert not IntimacyService.landmarks(c,e.wid,e.player,e.npc)
    response = e.client.get(f"/api/worlds/{e.wid}/player/experience")
    assert response.status_code == 200
    assert "私密意义标记" not in json.dumps(response.json(),ensure_ascii=False)


class ParsingModel:
    def __init__(self, data):
        self.data = data
    def complete(self, *, schema, **kwargs):
        return SimpleNamespace(data=schema.validate_python(self.data))


def test_natural_consent_preview_is_read_only_then_idempotent(town):
    e = town
    with e.db.write() as c:
        npc=c.execute("SELECT * FROM characters WHERE id=?",(e.npc,)).fetchone()
        player=c.execute("SELECT * FROM characters WHERE id=?",(e.player,)).fetchone()
        proposal=SharedLifeService.apply(c,npc,player,e.at,SharedLifeIntent(operation="invite",kind="talk",statement="我们一起聊十五分钟好吗？"))
    engine = WorldEngine(e.db,e.settings,e.model)
    engine.intent_parser = IntentParserAgent(ParsingModel({"operation":"shared_life","target_character_id":e.npc,
        "choice_evidence":"我愿意和你聊十五分钟", "shared_life_choice":{"operation":"accept","moment_id":proposal["moment_id"],"statement":"我愿意和你聊十五分钟"}}))
    preview=engine.preview_player_intent(e.wid,"说话：我愿意和你聊十五分钟",target_character_id=e.npc)
    assert preview.route_kind == "social_choice"
    with e.db.read() as c:
        assert c.execute("SELECT status FROM shared_life_moments").fetchone()[0]=="pending"
    payload={"request_id":str(uuid4()),"recipient_id":e.npc,"intent":"我愿意和你聊十五分钟", "evidence":preview.choice_evidence,
             "shared_life_choice":preview.shared_life_choice.model_dump()}
    first=e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=payload)
    assert first.status_code==200, first.text
    assert first.json()["status"]=="active"
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=payload).json()==first.json()
    payload["intent"]="另一份选择"
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices",json=payload).status_code==422


@pytest.mark.parametrize("evidence,mid",[("别人的话","missing"),("我愿意和你聊十五分钟","missing")])
def test_parser_cannot_invent_evidence_or_invitation(town,evidence,mid):
    e=town
    engine=WorldEngine(e.db,e.settings,e.model)
    engine.intent_parser=IntentParserAgent(ParsingModel({"operation":"shared_life","target_character_id":e.npc,
        "choice_evidence":evidence,"shared_life_choice":{"operation":"accept","moment_id":mid,"statement":"我愿意"}}))
    preview=engine.preview_player_intent(e.wid,"说话：我愿意和你聊十五分钟",target_character_id=e.npc)
    assert preview.route_kind=="speech"


def test_old_landmark_survives_and_unknown_names_stay_unknown(town):
    e=town
    with e.db.write() as c:
        for index in range(43):
            summary="林澈参与的一次相处" if index==0 else f"后来的第{index}次关系转折"
            at=e.at+timedelta(minutes=index)
            event=ActionService._record_event(c,world_id=e.wid,tick_id=str(uuid4()),occurred_at=at,
                event_type="social.memory_test",actor_id=e.npc,target_id=e.player,location_id=e.loc["id"],summary=summary,payload={})
            IntimacyService.remember_event(c,e.wid,e.npc,e.player,event,at,"shared" if index==0 else "milestone",summary)
            if index==0:
                earliest=event
        # 哥哥，四十三次经历全部已经发生，才检验最早往事仍能保留。
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at),e.wid))
        memories=IntimacyService.landmarks(c,e.wid,e.player,e.npc)
        assert any(row["source_event_id"]==earliest for row in memories)
        assert "林澈" not in " ".join(row["meaning"] for row in memories)
    response=e.client.get(f"/api/worlds/{e.wid}/relationships/{e.npc}/memories")
    assert response.status_code==200
    assert len(response.json())==4


def test_confirmed_relationship_is_the_public_profile_stage(town):
    e=town
    with e.db.write() as c:
        npc=c.execute("SELECT * FROM characters WHERE id=?",(e.npc,)).fetchone()
        player=c.execute("SELECT * FROM characters WHERE id=?",(e.player,)).fetchone()
        proposal=IntimacyService.apply(c,npc,player,e.at,RelationshipIntent(operation="propose",kind="close_friend",statement="成为挚友吧"))
        IntimacyService.apply(c,player,npc,e.at,RelationshipIntent(operation="accept",bond_id=proposal["bond_id"],statement="我也愿意"))
    response=e.client.get(f"/api/worlds/{e.wid}/player/experience")
    person=next(row for row in response.json()["known_people"] if row["id"]==e.npc)
    assert person["stage"]=="挚友"


def test_urgent_hunger_releases_both_people(town):
    e=town
    proposal=invite(e)
    assert respond(e,proposal["moment_id"]).action_status=="active"
    with e.db.write() as c:
        c.execute("UPDATE characters SET satiety=0 WHERE id=?",(e.npc,))
        SharedLifeService.maintain(c,e.wid,e.at+timedelta(minutes=1))
        assert c.execute("SELECT status FROM shared_life_moments").fetchone()[0]=="interrupted"
        assert not c.execute("SELECT 1 FROM character_life_activities WHERE status='running'").fetchone()
