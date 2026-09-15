"""独立存档中的七日生活、认知边界与可恢复动作回归。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.actions import ActionService
from world_engine.api import create_app
from world_engine.clock import WorldClockService
from world_engine.database import Database
from world_engine.decisions import NpcReply, RuleDecisionProvider
from world_engine.engine import WorldEngine
from world_engine.outreach import process_outreach
from world_engine.repository import from_iso, to_iso
from world_engine.society import SocietyService


class LivingModel(RuleDecisionProvider):
    """仅在隔离验收中替代模型，生产路径没有预设回复。"""

    def respond_to_player(self, *, npc, player, context):
        return NpcReply(reply=f"我叫{npc.name}，今天也在这里忙碌。", social_move="answer")

    def complete(self, *, schema, label, **kwargs):
        data = {"reply": "我记得我们的约定，想和你说说近况。"}
        if label == "npc_outreach":
            data["should_contact"] = True
        return SimpleNamespace(data=schema.validate_python(data))


@pytest.fixture
def town(settings, monkeypatch):
    model = LivingModel()
    monkeypatch.setattr("world_engine.engine.build_decision_provider", lambda _: model)
    monkeypatch.setattr("world_engine.engine.build_agent_model_backend", lambda _: model)
    db = Database(settings.database_path)
    with TestClient(create_app(settings)) as client:
        initial = client.post("/api/worlds", json={"name": "独立七日生活验收"}).json()
        wid = initial["world"]["id"]
        npc = next(p for p in initial["characters"] if p["name"] == "林澈")
        loc = next(p for p in initial["locations"] if p["id"] == npc["location_id"])
        response = client.post(
            f"/api/worlds/{wid}/player",
            json={"name": "七日旅人", "identity": "旅人", "location_id": loc["id"]},
        )
        assert response.status_code == 201, response.text
        player = next(p for p in response.json()["characters"] if p["is_player"])
        at = datetime(2030, 1, 1, 8, tzinfo=UTC)
        with db.write() as c:
            c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), wid))
            c.execute(
                "UPDATE world_clock SET time_scale=1,next_adjudication_world_time=? WHERE world_id=?",
                (to_iso(at + timedelta(days=10)), wid),
            )
            c.execute(
                "UPDATE characters SET energy=90,satiety=100,money=200,inventory_capacity=40 WHERE world_id=?",
                (wid,),
            )
            c.execute(
                "UPDATE characters SET longitude=?,latitude=?,current_location_id=?,identity='商人',skills_json='[\"修理\"]' WHERE id=?",
                (loc["longitude"], loc["latitude"], loc["id"], npc["id"]),
            )
        env = SimpleNamespace(
            client=client,
            db=db,
            wid=wid,
            player=player["id"],
            npc=npc["id"],
            loc=loc,
            clock=WorldClockService(db, settings),
            model=model,
            settings=settings,
            at=at,
        )
        env.food = definition(
            env,
            {
                "element_type": "commodity",
                "name": "旅行面包",
                "category": "food",
                "price": 3,
                "nutrition": 60,
                "stack_limit": 100,
                "resource_location_id": loc["id"],
                "resource_key": "bakery_supply",
                "resource_owner_id": npc["id"],
                "initial_resource": 50,
                "daily_growth": 20,
                "resource_capacity": 100,
            },
        )
        definition(
            env,
            {
                "element_type": "workplace_budget",
                "location_id": loc["id"],
                "initial_funds": 5000,
                "wage": 12,
            },
        )
        with db.write() as c:
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,owner_character_id,quantity,condition) VALUES (?,?,?,'character_inventory',?,?,30,100)",
                (str(uuid4()), wid, env.food, npc["id"], npc["id"]),
            )
        yield env


def definition(env, payload):
    proposed = env.client.post(
        f"/api/worlds/{env.wid}/economy-definitions",
        json={"idempotency_key": str(uuid4()), "payload": payload},
    )
    assert proposed.status_code == 201, proposed.text
    rid = proposed.json()["registration"]["id"]
    applied = env.client.post(f"/api/worlds/{env.wid}/registrations/{rid}/confirm")
    assert applied.status_code == 200 and applied.json()["status"] == "applied", applied.text
    return applied.json()["result_entity_id"]


def post(env, path, payload=None, status=200):
    response = env.client.post(f"/api/worlds/{env.wid}/{path}", json=payload)
    assert response.status_code == status, response.text
    return response.json()


def lodging(env):
    with env.db.read() as c:
        money_before = c.execute("SELECT money FROM characters WHERE id=?",(env.player,)).fetchone()[0]
    room = {
        "name": "河边客房",
        "location_id": env.loc["id"],
        "owner_character_id": env.npc,
        "nightly_rate": 2,
        "fixtures": [{"name": "行李柜", "kind": "container", "capacity": 20}],
    }
    proposed = post(env, "interior-layouts", {"idempotency_key": str(uuid4()), "room": room}, 201)
    rid = proposed["registration"]["id"]
    applied = post(env, f"registrations/{rid}/confirm")
    assert applied["status"] == "applied", applied
    room_id = applied["result_entity_id"]
    request = post(
        env,
        "long-term-requests",
        {
            "recipient_id": env.npc,
            "operation_type": "住房",
            "terms": {
                "title": "租住河边客房",
                "room_id": room_id,
                "duration_days": 8,
                "payment": 16,
            },
        },
    )
    post(env, f"long-term-requests/{request['id']}/confirm", {"accept_counter_terms": True})
    with env.db.read() as c:
        saved = c.execute("SELECT terms_json,counter_terms_json FROM long_term_operation_requests WHERE id=?",(request["id"],)).fetchone()
        terms = json.loads(saved["counter_terms_json"] or saved["terms_json"])
        money_after = c.execute("SELECT money FROM characters WHERE id=?",(env.player,)).fetchone()[0]
        assert money_before-money_after == terms["payment"]
    return room_id


def eat_bought_food(env):
    market = env.client.get(f"/api/worlds/{env.wid}/player/market").json()
    offer = next(p for p in market["offers"] if p["seller_id"] == env.npc)
    payload = {
        "seller_id": env.npc,
        "item_id": offer["item_id"],
        "expected_total": offer["price"],
        "request_id": str(uuid4()),
    }
    first = post(env, "player/trade", payload)
    assert post(env, "player/trade", payload) == first
    result = post(env, "player/act", {"intent": "动作：吃饭"}, 201)
    return result


def hourly(env, hours):
    for _ in range(hours):
        env.clock.heartbeat(env.wid, elapsed_seconds=3600)


def test_seven_world_days_keep_lodging_work_food_storage_and_goals(town):
    e = town
    post(e, "contacts", {"recipient_id": e.npc})
    room = lodging(e)
    learning = post(e,"long-term-requests",{"recipient_id":e.npc,"operation_type":"学习","terms":{"title":"向居民学习修理","skill":"修理","payment":0}})
    post(e,f"long-term-requests/{learning['id']}/confirm",{"accept_counter_terms":True})
    post(e,"player/life-goals",{"kind":"learn","title":"学会基础修理","target_id":"修理","quantity":10},201)
    post(
        e,
        "player/life-goals",
        {"kind": "work", "title": "在镇上工作七次", "target_id": e.loc["id"], "quantity": 7},
        201,
    )
    post(
        e,
        "player/life-goals",
        {"kind": "reside", "title": "住满七天", "target_id": room, "quantity": 7},
        201,
    )
    for day in range(7):
        eat_bought_food(e)
        if day != 1:
            post(
                e,
                "player/act",
                {"intent": "说话：又见面了，今天也在镇上生活。", "target_character_id": e.npc},
                201,
            )
        if day == 0:
            appointment = post(e,"long-term-requests",{"recipient_id":e.npc,"operation_type":"约定","terms":{"title":"明日碰面","location_id":e.loc["id"],"duration_days":1,"payment":0}})
            post(e,f"long-term-requests/{appointment['id']}/confirm",{"accept_counter_terms":True})
        start = post(e, "player/life/tasks", {"request_id": str(uuid4())}, 201)
        hourly(e, 1)
        scene = e.client.get(f"/api/worlds/{e.wid}/player/life").json()
        assert scene["last_activity"]["id"] == start["activity_id"]
        assert scene["last_activity"]["status"] == "completed"
        eat_bought_food(e)
        post(e, f"player/life/doors/{room}", {"operation": "open"})
        post(e, f"player/life/doors/{room}", {"operation": "enter"})
        scene = e.client.get(f"/api/worlds/{e.wid}/player/life").json()
        fixture = scene["interior"]["fixtures"][0]["id"]
        post(e, f"player/life/fixtures/{fixture}", {"operation": "open"})
        own = next(
            (p for p in scene["items"] if p["place"] == "bag" and p["ownership"] == "self"), None
        )
        if day == 0 and own:
            stored = own["id"]
            post(
                e,
                f"player/life/fixtures/{fixture}/items",
                {"item_id": stored, "quantity": 1, "operation": "deposit"},
            )
        post(e, "player/life/activities", {"kind": "rest", "duration_minutes": 720}, 201)
        hourly(e, 12)
        post(e, f"player/life/doors/{room}", {"operation": "exit"})
        eat_bought_food(e)
        post(e, "player/life/activities", {"kind": "wait", "duration_minutes": 660}, 201)
        hourly(e, 11)
        # 重复读取和迁移不重复发薪、不改变世界时间或产生额外物品。
        before = e.client.get(f"/api/worlds/{e.wid}").json()["world"]["current_time"]
        e.db.initialize()
        assert e.client.get(f"/api/worlds/{e.wid}").json()["world"]["current_time"] == before
    experience = e.client.get(f"/api/worlds/{e.wid}/player/experience").json()
    assert from_iso(experience["world_time"]) - e.at == timedelta(days=7)
    assert all(goal["achieved"] for goal in experience["goals"])
    assert experience["residences"][0]["status"] == "active"
    assert experience["work_history"][0]["completed_units"] == 7
    assert experience["notifications"]
    with e.db.read() as c:
        assert c.execute("SELECT status FROM contract_fulfillments WHERE request_id=?",(appointment["id"],)).fetchone()[0] == "overdue"
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
        assert (
            c.execute(
                "SELECT count(*) FROM npc_daily_states WHERE last_slot IS NOT NULL"
            ).fetchone()[0]
            > 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM world_events WHERE actor_id=? AND event_type='action.npc_activity_started'",
                (e.npc,),
            ).fetchone()[0]
            > 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM item_instances WHERE container_type='fixture_storage' AND owner_character_id=?",
                (e.player,),
            ).fetchone()[0]
            > 0
        )
        assert (
            c.execute(
                "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
            ).fetchone()[0]
            < 5000
        )
        assert (
            c.execute(
                "SELECT count(*) FROM character_acquaintances WHERE observer_id=? AND known_name='林澈'",
                (e.player,),
            ).fetchone()[0]
            == 1
        )


def test_sequence_wait_resume_refresh_and_idempotency(town):
    e = town
    preview = post(e,"player/intents/preview",{"intent":"动作：休息1分钟\n动作：观察四周"})
    assert [step["kind"] for step in preview["sequence_steps"]] == ["action","action"]
    payload = {
        "request_id": str(uuid4()),
        "steps": [{"kind": "action", "text": "休息1分钟"}, {"kind": "action", "text": "观察四周"}],
    }
    first = post(e, "player/sequence", payload)
    assert first["status"] == "waiting" and len(first["results"]) == 1
    pending = e.client.get(f"/api/worlds/{e.wid}/player/sequences").json()
    assert pending[0]["next_index"] == 1
    e.db.initialize()
    e.clock.heartbeat(e.wid, elapsed_seconds=60)
    finished = post(e, "player/sequence", payload)
    assert finished["status"] == "completed" and len(finished["results"]) == 2
    assert post(e, "player/sequence", payload) == finished


def test_knowledge_never_infers_hidden_observers_and_whispers_do_not_spread(town):
    e = town
    with e.db.write() as c:
        outsider = c.execute(
            "SELECT id FROM characters WHERE world_id=? AND id NOT IN (?,?) LIMIT 1",
            (e.wid, e.player, e.npc),
        ).fetchone()[0]
        c.execute("UPDATE characters SET longitude=longitude+1 WHERE id=?", (outsider,))
        private = ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="action.socialize",
            actor_id=e.npc,
            target_id=e.player,
            location_id=e.loc["id"],
            summary="林澈悄声分享消息。",
            payload={"delivery": "whisper", "dialogue": "只有这里的人能听见。"},
        )
        assert not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (outsider, private),
        ).fetchone()
        SocietyService.exchange_news(c, e.wid, e.player, outsider, e.at)
        assert not c.execute(
            "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=?",
            (outsider, private),
        ).fetchone()
        context = SocietyService.personal_context(c, e.wid, e.player, e.at)
        assert all("林澈" not in item["summary"] for item in context["known_events"])
    post(e, "contacts", {"recipient_id": e.npc})
    assert any(
        person["name"] == "林澈"
        for person in e.client.get(f"/api/worlds/{e.wid}/player/experience").json()["known_people"]
    )


def test_outreach_model_failure_writes_no_speech_and_success_is_once(town):
    e = town
    post(e, "contacts", {"recipient_id": e.npc})
    engine = WorldEngine(e.db, e.settings)
    with e.db.write() as c:
        contact = c.execute(
            "SELECT id FROM character_contacts WHERE world_id=? LIMIT 1", (e.wid,)
        ).fetchone()[0]
        job = str(uuid4())
        c.execute(
            "INSERT INTO npc_outreach_jobs(id,world_id,npc_id,player_id,contact_id,channel,reason,world_day) VALUES (?,?,?,?,?,'letter','共同事务',?)",
            (job, e.wid, e.npc, e.player, contact, e.at.date().isoformat()),
        )

    class BrokenModel:
        def complete(self, **kwargs):
            raise RuntimeError("隔离模型故障")

    engine.agent_model_backend = BrokenModel()
    assert process_outreach(engine, e.wid) == 0
    with e.db.write() as c:
        assert not c.execute("SELECT 1 FROM character_messages WHERE id=?", (job,)).fetchone()
        c.execute("UPDATE npc_outreach_jobs SET status='pending' WHERE id=?", (job,))
    engine.agent_model_backend = e.model
    assert process_outreach(engine, e.wid) == 1
    assert process_outreach(engine, e.wid) == 0
    with e.db.read() as c:
        assert (
            c.execute("SELECT count(*) FROM character_messages WHERE id=?", (job,)).fetchone()[0]
            == 1
        )


def test_registered_prices_and_wage_reservations_are_authoritative(town):
    from world_engine.intent_effects import IntentEffectService

    e = town
    with e.db.write() as c:
        event = ActionService._record_event(
            c,
            world_id=e.wid,
            tick_id=str(uuid4()),
            occurred_at=e.at,
            event_type="action.socialize",
            actor_id=e.player,
            target_id=e.npc,
            location_id=e.loc["id"],
            summary="议价",
            payload={},
        )
        result = IntentEffectService().apply(
            c,
            world_id=e.wid,
            source_event_id=event,
            actor_id=e.player,
            target_id=e.npc,
            intent="支付1铜币购买旅行面包",
        )
        assert result and not result[0].applied
        before = c.execute(
            "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
        ).fetchone()[0]
    start = post(e, "player/life/tasks", {"request_id": str(uuid4())}, 201)
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
            ).fetchone()[0]
            == before - 12
        )
    post(e, f"player/life/activities/{start['activity_id']}/stop")
    with e.db.read() as c:
        assert (
            c.execute(
                "SELECT balance FROM workplace_accounts WHERE location_id=?", (e.loc["id"],)
            ).fetchone()[0]
            == before
        )
