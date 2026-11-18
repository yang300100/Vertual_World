"""哥哥，请客须有新的双方同意，食物、身体、空间与来源均真实结算。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointments import people
from test_lived_world import town  # noqa: F401
from test_preparation_follow_through import prepared
from test_relationship_growth import ParsingModel, give_player_food, respond

from world_engine.api_intimacy import NaturalSocialChoice
from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent
from world_engine.intimacy import IntimacyService
from world_engine.meal_sharing import MealSharingService
from world_engine.repository import to_iso
from world_engine.shared_life import SharedLifeIntent, SharedLifeService


def invite(e, host="npc", **extra):
    with e.db.write() as c:
        player, npc = people(e, c)
        actor, other = (npc, player) if host == "npc" else (player, npc)
        return SharedLifeService.apply(c, actor, other, e.at, SharedLifeIntent(
            operation="invite", kind="meal", meal_supply="proposer", minutes=15,
            statement="我愿意提供双方各一份自己的随身食物，请你一起吃十五分钟，你愿意吗？", **extra))


def accept(e, mid, request_id=None):
    text = "我明确接受这次由你提供双方各一份食物的共同用餐，持续十五个世界分钟。"
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json={
        "request_id": request_id or str(uuid4()), "recipient_id": e.npc, "intent": text, "evidence": text,
        "shared_life_choice": {"operation": "accept", "moment_id": mid, "statement": text}})


def units(c, e, cid):
    return c.execute("SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE world_id=? AND item_type_id=? AND owner_character_id=?", (e.wid, e.food, cid)).fetchone()[0]


def test_npc_host_does_not_consume_until_player_accepts_then_real_food_time_memory(town):
    e = town
    invitation = invite(e)
    mid = invitation["moment_id"]
    with e.db.read() as c:
        assert units(c, e, e.npc) == 30 and units(c, e, e.player) == 0
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()
        terms = SharedLifeService.rows(c, e.wid, e.player, e.at, e.npc)[0]
        assert terms["meal_supply"] == "proposer" and terms["meal_provider_id"] == e.npc
        wallets = [tuple(r) for r in c.execute("SELECT id,money FROM characters ORDER BY id")]
    key = str(uuid4())
    response = accept(e, mid, key)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert accept(e, mid, key).json() == response.json()
    with e.db.read() as c:
        assert units(c, e, e.npc) == 28 and units(c, e, e.player) == 0
        servings = c.execute("SELECT * FROM shared_meal_servings WHERE moment_id=?", (mid,)).fetchall()
        assert len(servings) == 2 and all(r["provider_id"] == e.npc for r in servings)
        assert {r["eater_id"] for r in servings} == {e.player, e.npc}
        payload = json.loads(c.execute("SELECT payload_json FROM world_events WHERE id=?", (response.json()["event_id"],)).fetchone()[0])
        assert len(payload["meal_servings"]) == 2
        assert c.execute("SELECT COUNT(*) FROM inventory_changes WHERE source_event_id=?", (response.json()["event_id"],)).fetchone()[0] >= 1
        assert [tuple(r) for r in c.execute("SELECT id,money FROM characters ORDER BY id")] == wallets
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    with e.db.read() as c:
        assert c.execute("SELECT status FROM shared_life_moments WHERE id=?", (mid,)).fetchone()[0] == "completed"
        assert any("邀请者提供双方食物" in r["meaning"] for r in IntimacyService.landmarks(c, e.wid, e.player, e.npc))


def test_player_can_host_and_npc_independently_accepts(town):
    e = town
    give_player_food(e)
    mid = invite(e, "player")["moment_id"]
    assert respond(e, mid).action_status == "active"
    with e.db.read() as c:
        assert units(c, e, e.player) == 0 and units(c, e, e.npc) == 28
        assert {r[0] for r in c.execute("SELECT provider_id FROM shared_meal_servings")} == {e.player}


@pytest.mark.parametrize("resolution", ["decline", "withdraw", "expiry"])
def test_unaccepted_hospitality_never_transfers_or_consumes_food(town, resolution):
    e = town
    mid = invite(e)["moment_id"]
    with e.db.write() as c:
        player, npc = people(e, c)
        if resolution == "expiry":
            c.execute("UPDATE shared_life_moments SET expires_at=? WHERE id=?", (to_iso(e.at), mid))
        else:
            SharedLifeService.apply(c, player if resolution == "decline" else npc, npc if resolution == "decline" else player, e.at,
                SharedLifeIntent(operation="decline" if resolution == "decline" else "end", moment_id=mid, statement="这次先不了。"))
    assert accept(e, mid).status_code == 409
    with e.db.read() as c:
        assert units(c, e, e.npc) == 30 and not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()


@pytest.mark.parametrize("problem", ["one_left", "spoiled", "broken", "borrowed", "host_reserve", "guest_avoid", "full_bag", "different_room", "busy"])
def test_start_revalidates_resources_and_rolls_back_all_partial_effects(town, problem):
    from world_engine.life import LifeActivityService

    e = town
    mid = invite(e)["moment_id"]
    with e.db.write() as c:
        iid = c.execute("SELECT id FROM item_instances WHERE item_type_id=? AND owner_character_id=?", (e.food, e.npc)).fetchone()[0]
        if problem == "one_left":
            c.execute("UPDATE item_instances SET quantity=1 WHERE id=?", (iid,))
        elif problem == "spoiled":
            c.execute("UPDATE item_instances SET spoils_world_time=? WHERE id=?", (to_iso(e.at), iid))
        elif problem == "broken":
            c.execute("UPDATE item_instances SET condition=0 WHERE id=?", (iid,))
        elif problem == "borrowed":
            c.execute("UPDATE item_instances SET owner_character_id=? WHERE id=?", (e.player, iid))
        elif problem in {"host_reserve", "guest_avoid"}:
            c.execute("INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)", (e.npc if problem == "host_reserve" else e.player, iid, "reserve" if problem == "host_reserve" else "avoid", "本人饮食安排", to_iso(e.at)))
        elif problem == "full_bag":
            c.execute("UPDATE characters SET inventory_capacity=0 WHERE id=?", (e.player,))
        elif problem == "different_room":
            c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (e.player,))
        else:
            player, _ = people(e, c)
            LifeActivityService.start(c, player, e.at, "rest", 30)
        before_items = [tuple(r) for r in c.execute("SELECT * FROM item_instances ORDER BY id")]
        before_people = [tuple(r) for r in c.execute("SELECT id,satiety,energy,money FROM characters ORDER BY id")]
        before_activities = c.execute("SELECT COUNT(*) FROM character_life_activities").fetchone()[0]
        player, npc = people(e, c)
        # 哥哥，即便上层捕获异常后提交事务，也不能留下主人吃了一半的失败用餐。
        with pytest.raises(ValueError):
            SharedLifeService.apply(c, player, npc, e.at, SharedLifeIntent(operation="accept", moment_id=mid, statement="我愿意接受这次请客。"))
        assert [tuple(r) for r in c.execute("SELECT * FROM item_instances ORDER BY id")] == before_items
        assert [tuple(r) for r in c.execute("SELECT id,satiety,energy,money FROM characters ORDER BY id")] == before_people
        assert c.execute("SELECT COUNT(*) FROM character_life_activities").fetchone()[0] == before_activities
        assert not c.execute("SELECT 1 FROM shared_meal_servings").fetchone()
        assert c.execute("SELECT status FROM shared_life_moments WHERE id=?", (mid,)).fetchone()[0] == "pending"


def test_interruption_keeps_consumption_and_receipts_without_creating_debt(town):
    e = town
    mid = invite(e)["moment_id"]
    assert accept(e, mid).status_code == 200
    with e.db.write() as c:
        player, npc = people(e, c)
        result = SharedLifeService.apply(c, player, npc, e.at + timedelta(minutes=1), SharedLifeIntent(operation="end", moment_id=mid, statement="我先结束。"))
        assert result["status"] == "interrupted" and "不形成欠款" in result["summary"]
        assert units(c, e, e.npc) == 28
        assert c.execute("SELECT COUNT(*) FROM shared_meal_servings WHERE moment_id=?", (mid,)).fetchone()[0] == 2


def test_hosted_meal_can_follow_preparation_without_taking_guests_food(town):
    e = town
    pid, _ = prepared(e)
    mid = invite(e, preparation_plan_id=pid)["moment_id"]
    assert accept(e, mid).status_code == 200
    with e.db.read() as c:
        assert units(c, e, e.player) == 1
        assert c.execute("SELECT plan_id FROM preparation_follow_through WHERE moment_id=?", (mid,)).fetchone()[0] == pid
        context = IntimacyService.context(c, e.wid, e.npc, e.player, e.at)
        moment = context["shared_moments"][0]
        assert moment["meal_supply"] == "proposer" and moment["meal_provider_id"] == e.npc
        assert context["shared_plans"][0]["follow_through"][0]["meal_supply"] == "proposer"


@pytest.mark.parametrize("fields", [{"operation": "invite", "kind": "talk"}, {"operation": "accept", "moment_id": "m"}, {"operation": "end", "moment_id": "m"}])
def test_response_cannot_rewrite_terms_or_extend_hosting_to_other_activities(fields):
    with pytest.raises(ValueError):
        SharedLifeIntent(**fields, meal_supply="proposer", statement="我愿意。")


@pytest.mark.parametrize("legacy_source", [True, False])
def test_historical_each_own_receipt_survives_hosting_default(legacy_source, town):
    e = town
    text = "我邀请你现在一起吃饭，各吃各的。"
    payload = NaturalSocialChoice(request_id=str(uuid4()), recipient_id=e.npc, intent=text, evidence=text,
        shared_life_choice=SharedLifeIntent(operation="invite", kind="meal", statement=text)).model_dump(mode="json")
    payload["shared_life_choice"].pop("meal_supply")
    if legacy_source:
        payload["shared_life_choice"].pop("preparation_plan_id")
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    saved = {"status": "pending", "moment_id": "historical", "event_id": "historical", "summary": "原选择已保存"}
    with e.db.write() as c:
        c.execute("INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)", (e.wid, payload["request_id"], e.player, digest, json.dumps(saved)))
    assert e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload).json() == saved


def test_parser_exposes_only_players_own_hosting_intent_and_preview_is_read_only(town):
    e = town
    text = "我愿意用自己的两份食物请你一起吃十五分钟。"
    engine = WorldEngine(e.db, e.settings, e.model)
    engine.intent_parser = IntentParserAgent(ParsingModel({"operation": "shared_life", "target_character_id": e.npc, "choice_evidence": text,
        "shared_life_choice": {"operation": "invite", "kind": "meal", "meal_supply": "proposer", "statement": text}}))
    preview = engine.preview_player_intent(e.wid, "说话：" + text, target_character_id=e.npc)
    assert preview.shared_life_choice.meal_supply == "proposer"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM shared_meal_offers").fetchone()


def test_null_supply_keeps_each_own_and_never_implies_hosting():
    choice = SharedLifeIntent(operation="invite", kind="meal", meal_supply=None, minutes=None, statement="一起吃饭吗？")
    assert choice.meal_supply == "each_own" and choice.minutes == 15


def test_guest_restriction_uses_original_batch_even_when_transfer_would_split(town):
    e = town
    with e.db.write() as c:
        player, npc = people(e, c)
        iid = c.execute("SELECT id FROM item_instances WHERE item_type_id=? AND owner_character_id=?", (e.food, e.npc)).fetchone()[0]
        c.execute("INSERT INTO collective_food_decisions VALUES (?,?,?,?,?)", (e.player, iid, "avoid", "本人安排", to_iso(e.at)))
        assert MealSharingService.foods(c, npc, e.at)
        assert not MealSharingService.foods(c, npc, e.at, player)


@pytest.mark.parametrize("quantity", [2, 30])
def test_hosting_preserves_tracked_materials_and_body_destinations(town, quantity):
    from world_engine.material_physics import MaterialState
    from world_engine.materials import MaterialService

    e = town
    with e.db.write() as c:
        item = c.execute("SELECT * FROM item_instances WHERE item_type_id=? AND owner_character_id=?", (e.food, e.npc)).fetchone()
        c.execute("UPDATE item_instances SET quantity=? WHERE id=?", (quantity, item["id"]))
        MaterialService.attach(c, item["id"], MaterialState(mass=quantity, nutrition=quantity * 60))
    mid = invite(e)["moment_id"]
    response = accept(e, mid)
    assert response.status_code == 200, response.text
    with e.db.read() as c:
        assert units(c, e, e.npc) == quantity - 2
        sinks = c.execute("SELECT destination,state_json FROM material_sinks WHERE world_id=?", (e.wid,)).fetchall()
        assert {r[0] for r in sinks} == {"body:" + e.player, "body:" + e.npc}
        assert sum(json.loads(r[1])["mass"] for r in sinks) == pytest.approx(2)
