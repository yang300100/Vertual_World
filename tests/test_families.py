"""哥哥，验证家庭承诺、真实住房、资金守恒和经过登记的亲子关系。"""

# ruff: noqa: F811
from datetime import timedelta
from uuid import uuid4

import pytest
from test_lived_world import town  # noqa: F401

from world_engine.decisions import NpcReply
from world_engine.actions import ActionService
from world_engine.families import FamilyIntent,FamilyService
from world_engine.intimacy import IntimacyService,RelationshipIntent
from world_engine.interiors import InteriorService
from world_engine.npc_performance import settle_npc_reply
from world_engine.repository import from_iso,to_iso
from world_engine.registration import WorldElementRegistry,ElementRegistrationSubmit,CharacterBirthSpec


@pytest.fixture(autouse=True)
def declared_adult_player(town):
    # 隔离存档中的玩家明确声明成年资料，不让生产代码替未知年龄作推定。
    response=town.client.post(f"/api/worlds/{town.wid}/player/life-details",json={"age_years":30})
    assert response.status_code==200,response.text
    with town.db.write() as c:
        c.execute("UPDATE characters SET birth_world_time=? WHERE id=?",(to_iso(town.at-timedelta(days=365*30)),town.npc))


def room(e):
    r=e.client.post(f"/api/worlds/{e.wid}/interior-layouts",json={"idempotency_key":str(uuid4()),"room":{
        "name":"共同住所","location_id":e.loc["id"],"owner_character_id":e.npc,"access_policy":"private"}})
    assert r.status_code==201,r.text
    rid=r.json()["registration"]["id"]
    response=e.client.post(f"/api/worlds/{e.wid}/registrations/{rid}/confirm")
    assert response.json()["status"]=="applied",response.text
    return response.json()["result_entity_id"]


def choose(e,operation="propose",**kwargs):
    return e.client.post(f"/api/worlds/{e.wid}/families",json={"recipient_id":e.npc,"choice":{
        "operation":operation,"statement":"我希望认真商量并尊重彼此的选择。",**kwargs}})


def npc(e,operation,**kwargs):
    with e.db.write() as c:
        actor=c.execute("SELECT * FROM characters WHERE id=?",(e.npc,)).fetchone()
        return settle_npc_reply(c,npc=actor,player_id=e.player,at=e.at,channel="in_person",reply=NpcReply(
            reply="我也愿意认真维持这份安排。",family_intent=FamilyIntent(operation=operation,statement="我也愿意认真维持这份安排。",**kwargs)))


def establish(e,kind,**kwargs):
    p=choose(e,kind=kind,**kwargs)
    assert p.status_code==200,p.text
    aid=p.json()["agreement_id"]
    result=npc(e,"accept",agreement_id=aid)
    assert result.action_status=="active",result
    return aid


def romance(e):
    with e.db.write() as c:
        first=c.execute("SELECT * FROM characters WHERE id=?",(e.player,)).fetchone()
        second=c.execute("SELECT * FROM characters WHERE id=?",(e.npc,)).fetchone()
        p=IntimacyService.apply(c,first,second,e.at,RelationshipIntent(operation="propose",kind="romantic",statement="我想成为恋人"))
        IntimacyService.apply(c,second,first,e.at,RelationshipIntent(operation="accept",bond_id=p["bond_id"],statement="我也愿意"))


def household(e):
    rid=room(e)
    home=establish(e,"cohabit",room_id=rid)
    family=establish(e,"family",title="河边之家")
    return rid,home,family


def test_marriage_needs_separate_consent_and_does_not_grant_property(town):
    e=town
    assert choose(e,kind="marriage").status_code==409
    romance(e)
    proposal=choose(e,kind="marriage").json()
    assert choose(e,"accept",agreement_id=proposal["agreement_id"]).status_code==409
    assert npc(e,"accept",agreement_id=proposal["agreement_id"]).action_status=="active"
    living=e.client.get(f"/api/worlds/{e.wid}/player/experience").json()
    assert next(p for p in living["known_people"] if p["id"]==e.npc)["stage"]=="配偶"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM home_residency_permissions WHERE resident_id=?",(e.player,)).fetchone()
    assert choose(e,"end",agreement_id=proposal["agreement_id"]).json()["status"]=="ended"


def test_cohabitation_does_not_teleport_and_exit_preserves_separate_permissions(town):
    e=town
    rid=room(e)
    with e.db.read() as c:
        r=InteriorService.room(c,e.wid,rid)
        assert not InteriorService.keyed(c,r,e.player)
        position=tuple(c.execute("SELECT longitude,latitude,current_room_id FROM characters WHERE id=?",(e.player,)).fetchone())
    aid=establish(e,"cohabit",room_id=rid)
    with e.db.write() as c:
        r=InteriorService.room(c,e.wid,rid)
        assert InteriorService.keyed(c,r,e.player)
        assert not InteriorService.storage_allowed(c,r,e.player)
        assert tuple(c.execute("SELECT longitude,latitude,current_room_id FROM characters WHERE id=?",(e.player,)).fetchone())==position
        c.execute("INSERT INTO life_room_access VALUES (?,?)",(rid,e.player))
    choose(e,"end",agreement_id=aid)
    with e.db.read() as c:
        assert InteriorService.keyed(c,InteriorService.room(c,e.wid,rid),e.player)
        assert c.execute("SELECT owner_character_id FROM life_rooms WHERE id=?",(rid,)).fetchone()[0]==e.npc


def test_family_money_conserved_and_only_jointly_disbursed(town):
    e=town
    _,_,family=household(e)
    def total():
        with e.db.read() as c:
            return c.execute("SELECT SUM(money) FROM characters WHERE world_id=?",(e.wid,)).fetchone()[0]+c.execute("SELECT COALESCE(SUM(balance),0) FROM household_accounts WHERE family_id=?",(family,)).fetchone()[0]
    before=total()
    assert choose(e,"contribute",agreement_id=family,amount=40).status_code==200
    assert npc(e,"contribute",agreement_id=family,amount=30).action_status=="applied"
    assert total()==before
    assert choose(e,"withdraw",agreement_id=family,amount=41).status_code==409
    assert choose(e,"withdraw",agreement_id=family,amount=20).status_code==200
    allowance=choose(e,kind="allowance",amount=25,title="采购粮食的预算").json()
    with e.db.read() as c:
        wallet=c.execute("SELECT money FROM characters WHERE id=?",(e.player,)).fetchone()[0]
    assert npc(e,"accept",agreement_id=allowance["agreement_id"]).action_status=="completed"
    with e.db.read() as c:
        assert c.execute("SELECT money FROM characters WHERE id=?",(e.player,)).fetchone()[0]==wallet+25
    assert npc(e,"accept",agreement_id=allowance["agreement_id"]).action_status=="rejected"
    assert choose(e,"end",agreement_id=family).json()["status"]=="ended"
    assert total()==before


def test_invalid_residency_and_owner_revocation_are_live(town):
    e=town
    rid=room(e)
    with e.db.write() as c:
        third=c.execute("SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",(e.wid,e.player,e.npc)).fetchone()[0]
        c.execute("UPDATE life_rooms SET owner_character_id=? WHERE id=?",(third,rid))
    assert choose(e,kind="cohabit",room_id=rid).status_code==409
    with e.db.write() as c:
        owner=c.execute("SELECT * FROM characters WHERE id=?",(third,)).fetchone()
        for cid in (e.player,e.npc):
            target=c.execute("SELECT * FROM characters WHERE id=?",(cid,)).fetchone()
            FamilyService.apply(c,owner,target,e.at,FamilyIntent(operation="authorize_residence",room_id=rid,statement="我同意你在这间屋居住"))
    home=establish(e,"cohabit",room_id=rid)
    with e.db.write() as c:
        owner=c.execute("SELECT * FROM characters WHERE id=?",(third,)).fetchone()
        player=c.execute("SELECT * FROM characters WHERE id=?",(e.player,)).fetchone()
        FamilyService.apply(c,owner,player,e.at,FamilyIntent(operation="revoke_residence",room_id=rid,statement="我决定撤回这份居住许可"))
        FamilyService.maintain(c,e.wid,e.at)
        assert c.execute("SELECT status FROM family_agreements WHERE id=?",(home,)).fetchone()[0]=="suspended"
        assert not InteriorService.keyed(c,InteriorService.room(c,e.wid,rid),e.player)


def test_parenthood_is_plan_first_then_registered_birth_and_real_care(town):
    e=town
    rid,_,family=household(e)
    with e.db.read() as c:before=c.execute("SELECT COUNT(*) FROM characters WHERE world_id=?",(e.wid,)).fetchone()[0]
    aid=establish(e,"parenthood",child_name="河芽",child_species="human")
    with e.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM characters WHERE world_id=?",(e.wid,)).fetchone()[0]==before
        plan=c.execute("SELECT p.* FROM family_plans p JOIN family_agreements a ON a.registration_id=p.registration_id WHERE a.id=?",(aid,)).fetchone()
        assert plan["status"]=="planned"
    assert choose(e,"register_birth",agreement_id=aid).status_code==409
    with e.db.write() as c:
        # 这里只推进登记验收的世界日期，不把它冒充正式存档里的生活模拟。
        e.at=from_iso(plan["due_at_world"])+timedelta(minutes=1)
        c.execute("UPDATE worlds SET current_time=? WHERE id=?",(to_iso(e.at),e.wid))
        c.execute("UPDATE characters SET current_room_id=? WHERE id IN (?,?)",(rid,e.player,e.npc))
    result=choose(e,"register_birth",agreement_id=aid)
    assert result.status_code==200,result.text
    child=result.json()["child_id"]
    with e.db.write() as c:
        assert c.execute("SELECT COUNT(*) FROM character_lineages WHERE child_character_id=?",(child,)).fetchone()[0]==2
        assert c.execute("SELECT current_room_id FROM characters WHERE id=?",(child,)).fetchone()[0]==rid
        assert FamilyService.is_dependent(c,child,e.at)
        c.execute("UPDATE characters SET satiety=20 WHERE id=?",(child,))
        quantity=c.execute("SELECT SUM(quantity) FROM item_instances WHERE owner_character_id=? AND item_type_id=?",(e.npc,e.food)).fetchone()[0]
        FamilyService.care_dependents(c,e.wid,e.at)
        assert c.execute("SELECT satiety FROM characters WHERE id=?",(child,)).fetchone()[0]>20
        assert c.execute("SELECT SUM(quantity) FROM item_instances WHERE owner_character_id=? AND item_type_id=?",(e.npc,e.food)).fetchone()[0]==quantity-1
    assert choose(e,"register_birth",agreement_id=aid).status_code==409


def test_direct_character_plan_cannot_assign_another_parent_without_consent(town):
    e=town
    with e.db.write() as c:
        source=ActionService._record_event(c,world_id=e.wid,tick_id=str(uuid4()),occurred_at=e.at,event_type="action.socialize",actor_id=e.player,target_id=e.npc,location_id=e.loc["id"],summary="普通交谈",payload={})
        result=WorldElementRegistry().submit(c,world_id=e.wid,request=ElementRegistrationSubmit(requested_by_character_id=e.player,source_event_id=source,idempotency_key=str(uuid4()),
            payload=CharacterBirthSpec(name="不能凭空决定的孩子",parent_character_ids=[e.player,e.npc],species="human")))
        assert result.status.value=="rejected"
        assert "双方" in result.rejection_reason


def test_player_age_is_explicit_and_cannot_rewrite_history(town):
    e=town
    response=e.client.post(f"/api/worlds/{e.wid}/player/life-details",json={"age_years":31})
    assert response.status_code==409


def test_parenthood_requires_same_family_when_terms_change(town):
    e=town
    _,_,family=household(e)
    proposal=choose(e,kind="parenthood",child_name="河芽",child_species="human").json()
    choose(e,"end",agreement_id=family)
    establish(e,"family",title="重新商量的家庭")
    assert npc(e,"accept",agreement_id=proposal["agreement_id"]).action_status=="rejected"


def test_player_profile_memories_preserve_unknown_names_and_original_audit(town):
    e=town
    with e.db.write() as c:
        event=ActionService._record_event(c,world_id=e.wid,tick_id=str(uuid4()),occurred_at=e.at,event_type="action.socialize",actor_id=e.player,target_id=e.npc,location_id=e.loc["id"],summary="七日旅人与林澈交谈。",payload={"dialogue":"你好","reply":"你好"})
        ActionService._record_memory(c,world_id=e.wid,character_id=e.player,event_id=event,memory_type="experienced",summary="七日旅人与林澈交谈。",importance=6)
    response=e.client.get(f"/api/worlds/{e.wid}/characters/{e.player}/memories")
    assert response.status_code==200,response.text
    assert all("林澈" not in memory["summary"] for memory in response.json())
    with e.db.read() as c:
        assert "林澈" in c.execute("SELECT summary FROM character_memories WHERE event_id=? AND character_id=?",(event,e.player)).fetchone()[0]
