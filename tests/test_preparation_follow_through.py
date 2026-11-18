"""哥哥，筹备之后的相处仍需双方同意，并留下真实消耗与经历。"""

# ruff: noqa: F811
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_appointments import people
from test_lived_world import town  # noqa: F401
from test_relationship_growth import ParsingModel, respond
from test_shared_plans import choose, get, offer, player_food

from world_engine.engine import WorldEngine
from world_engine.intent_parser import IntentParserAgent
from world_engine.intimacy import IntimacyService
from world_engine.repository import to_iso
from world_engine.shared_life import SharedLifeIntent, SharedLifeService
from world_engine.shared_plans import SharedPlanIntent, SharedPlanService


def prepared(e):
    pid = offer(e, own={"kind": "prepare_food", "target": 1})
    iid = player_food(e)
    assert choose(e, "accept", pid).status_code == 200
    assert choose(e, "check_in", pid).status_code == 200
    with e.db.write() as c:
        player, npc = people(e, c)
        SharedPlanService.apply(c, npc, player, e.at, SharedPlanIntent(
            operation="check_in", plan_id=pid, statement="我也到场核对自己的食物。"))
    assert get(e, pid)["status"] == "completed"
    return pid, iid


def follow(e, pid, kind="meal", request_id=None):
    text = "筹备已核对，我邀请你现在一起用餐十五分钟，各用自己的一份食物。"
    return e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json={
        "request_id": request_id or str(uuid4()), "recipient_id": e.npc,
        "intent": text, "evidence": text, "shared_life_choice": {
            "operation": "invite", "kind": kind, "preparation_plan_id": pid,
            "minutes": 15, "statement": text}})


def quantities(e):
    with e.db.read() as c:
        return {r["owner_character_id"]: r["n"] for r in c.execute(
            "SELECT owner_character_id,SUM(quantity) n FROM item_instances WHERE world_id=? AND item_type_id=? GROUP BY owner_character_id",
            (e.wid, e.food))}


def test_follow_through_needs_new_consent_and_records_real_food_time_and_memory(town):
    e = town
    pid, _ = prepared(e)
    before = quantities(e)
    request_id = str(uuid4())
    response = follow(e, pid, request_id=request_id)
    assert response.status_code == 200, response.text
    mid = response.json()["moment_id"]
    assert follow(e, pid, request_id=request_id).json() == response.json()
    assert quantities(e) == before
    assert get(e, pid)["follow_through"][0]["status"] == "pending"
    assert not get(e, pid)["can_follow_through"]
    with e.db.read() as c:
        wallets = [tuple(r) for r in c.execute("SELECT id,money FROM characters ORDER BY id")]
    assert respond(e, mid).action_status == "active"
    assert quantities(e) == {cid: n - 1 for cid, n in before.items() if n > 1}
    assert respond(e, mid).action_status == "rejected"
    with e.db.read() as c:
        stale = c.execute("SELECT * FROM shared_life_moments WHERE id=?", (mid,)).fetchone()
        assert [tuple(r) for r in c.execute("SELECT id,money FROM characters ORDER BY id")] == wallets
        assert c.execute("SELECT COUNT(*) FROM character_life_activities WHERE status='running'").fetchone()[0] == 2
    e.clock.heartbeat(e.wid, elapsed_seconds=900)
    assert get(e, pid)["follow_through"][0]["status"] == "completed"
    with e.db.write() as c:
        event = c.execute("SELECT * FROM world_events WHERE id=(SELECT source_event_id FROM shared_life_moments WHERE id=?)", (mid,)).fetchone()
        payload = json.loads(event["payload_json"])
        assert payload["preparation_plan_id"] == pid and payload["preparation_event_id"]
        assert any("共同筹备之后" in m["meaning"] for m in IntimacyService.landmarks(c, e.wid, e.player, e.npc))
        count = c.execute("SELECT SUM(shared_experiences) FROM character_acquaintances").fetchone()[0]
        SharedLifeService.finish(c, stale, e.at + timedelta(hours=1), "completed", "重复回调")
        assert c.execute("SELECT SUM(shared_experiences) FROM character_acquaintances").fetchone()[0] == count
    assert follow(e, pid).status_code == 409


@pytest.mark.parametrize("problem", ["consumed", "spoiled", "broken", "reserve", "foreign_owner"])
def test_old_preparation_cannot_replace_current_usable_food(town, problem):
    e = town
    pid, iid = prepared(e)
    response = follow(e, pid)
    with e.db.write() as c:
        if problem == "consumed":
            c.execute("DELETE FROM item_instances WHERE id=?", (iid,))
        elif problem == "spoiled":
            c.execute("UPDATE item_instances SET spoils_world_time=? WHERE id=?", (to_iso(e.at), iid))
        elif problem == "broken":
            c.execute("UPDATE item_instances SET condition=0 WHERE id=?", (iid,))
        elif problem == "reserve":
            c.execute("INSERT INTO collective_food_decisions(batch_id,actor_id,policy,reason,decided_at) VALUES (?,?,'reserve','本人留存',?)", (iid, e.player, to_iso(e.at)))
        else:
            c.execute("UPDATE item_instances SET owner_character_id=? WHERE id=?", (e.npc, iid))
    before = quantities(e)
    assert respond(e, response.json()["moment_id"]).action_status == "rejected"
    assert quantities(e) == before
    assert get(e, pid)["follow_through"][0]["status"] == "pending"
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM character_life_activities WHERE status='running'").fetchone()


@pytest.mark.parametrize("resolution", ["decline", "end", "expiry"])
def test_unstarted_invitation_can_be_declined_withdrawn_or_expire_then_renegotiated(town, resolution):
    e = town
    pid, _ = prepared(e)
    before = quantities(e)
    mid = follow(e, pid).json()["moment_id"]
    if resolution == "decline":
        assert respond(e, mid, "decline").action_status == "declined"
    elif resolution == "end":
        with e.db.write() as c:
            player, npc = people(e, c)
            SharedLifeService.apply(c, player, npc, e.at, SharedLifeIntent(operation="end", moment_id=mid, statement="我先撤回。"))
    else:
        with e.db.write() as c:
            c.execute("UPDATE shared_life_moments SET expires_at=? WHERE id=?", (to_iso(e.at), mid))
    assert get(e, pid)["can_follow_through"]
    assert follow(e, pid).status_code == 200
    assert quantities(e) == before


def test_interruption_preserves_consumption_and_does_not_redeem_preparation_twice(town):
    e = town
    pid, _ = prepared(e)
    mid = follow(e, pid).json()["moment_id"]
    assert respond(e, mid).action_status == "active"
    after = quantities(e)
    with e.db.write() as c:
        player, npc = people(e, c)
        SharedLifeService.apply(c, player, npc, e.at, SharedLifeIntent(operation="end", moment_id=mid, statement="我先离开。"))
    assert quantities(e) == after
    assert get(e, pid)["follow_through"][0]["status"] == "interrupted"
    assert follow(e, pid).status_code == 409
    # 哥哥，原筹备只能衔接一次已开始的活动，双方仍可另行邀请普通相处。
    assert follow(e, None, kind="talk").status_code == 200


@pytest.mark.parametrize("state", ["pending", "active", "ended", "expired"])
def test_uncompleted_or_missing_plan_cannot_be_used(town, state):
    e = town
    pid = offer(e)
    if state != "pending":
        with e.db.write() as c:
            c.execute("UPDATE shared_preparation_plans SET status=? WHERE id=?", (state, pid))
    assert follow(e, pid).status_code == 409
    assert follow(e, "invented").status_code == 409
    with e.db.read() as c:
        assert not c.execute("SELECT 1 FROM preparation_follow_through").fetchone()


def test_another_person_cannot_use_the_pairs_preparation(town):
    e = town
    pid, _ = prepared(e)
    with e.db.write() as c:
        player, _ = people(e, c)
        third = c.execute("SELECT * FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1", (e.wid, e.player, e.npc)).fetchone()
        with pytest.raises(ValueError, match="双方"):
            SharedLifeService._preparation(c, player, third, pid)


@pytest.mark.parametrize("operation,kind", [("accept", None), ("invite", "hug"), ("invite", "hold_hands"), ("end", None)])
def test_preparation_link_does_not_expand_consent_or_replace_an_existing_invitation(operation, kind):
    with pytest.raises(ValueError):
        SharedLifeIntent(operation=operation, kind=kind, moment_id=None if operation == "invite" else "m", preparation_plan_id="p", statement="我愿意。")


@pytest.mark.parametrize("pid_choice", ["actual", "invented", "uncompleted"])
def test_natural_parser_requires_a_known_completed_source(town, pid_choice):
    e = town
    pid, _ = prepared(e)
    if pid_choice == "uncompleted":
        with e.db.write() as c:
            c.execute("UPDATE shared_preparation_plans SET status='active' WHERE id=?", (pid,))
    engine = WorldEngine(e.db, e.settings, e.model)
    text = "筹备已完成，我邀请你现在共同用餐十五分钟。"
    engine.intent_parser = IntentParserAgent(ParsingModel({"operation": "shared_life", "target_character_id": e.npc,
        "choice_evidence": text, "shared_life_choice": {"operation": "invite", "kind": "meal", "minutes": 15,
        "preparation_plan_id": "invented" if pid_choice == "invented" else pid, "statement": text}}))
    preview = engine.preview_player_intent(e.wid, "说话：" + text, target_character_id=e.npc)
    assert (preview.route_kind == "social_choice") == (pid_choice == "actual")
    assert get(e, pid)["follow_through"] == []


def test_legacy_shared_life_receipt_survives_new_null_source_field(town):
    from world_engine.api_intimacy import NaturalSocialChoice

    e = town
    text = "我愿意现在邀请你共同聊天。"
    payload = NaturalSocialChoice(request_id=str(uuid4()), recipient_id=e.npc, intent=text, evidence=text,
        shared_life_choice=SharedLifeIntent(operation="invite", kind="talk", statement=text)).model_dump(mode="json")
    payload["shared_life_choice"].pop("preparation_plan_id")
    for key in ("family_choice", "appointment_choice", "shared_plan_choice"):
        payload.pop(key)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    receipt = {"status": "pending", "moment_id": "historic", "event_id": "historic", "summary": "旧邀请已记录"}
    with e.db.write() as c:
        c.execute("INSERT INTO social_choice_receipts VALUES (?,?,?,?,?)", (e.wid, payload["request_id"], e.player, digest, json.dumps(receipt)))
    response = e.client.post(f"/api/worlds/{e.wid}/player/social-choices", json=payload)
    assert response.json() == receipt


def test_completed_preparation_offers_npc_discretionary_follow_up_once(town):
    from world_engine.society import SocietyService

    e = town
    pid, _ = prepared(e)
    with e.db.write() as c:
        SocietyService.tick(c, e.wid, e.at)
        job = c.execute("SELECT reason FROM npc_outreach_jobs WHERE npc_id=? AND player_id=?", (e.npc, e.player)).fetchone()
        assert job and "另行邀请" in job[0] and "独立同意" in job[0]
    assert follow(e, pid).status_code == 200
