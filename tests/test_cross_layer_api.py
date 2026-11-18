"""哥哥，接口只提交玩家本人选择，不接收或代签 NPC 身份。"""
from contextlib import contextmanager
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from world_engine.api_cross_layer import build_cross_layer_router
from world_engine.cross_layer import CrossLayerPassageService
from test_cross_layer import prepared, wonder_world, at, talk  # noqa: F401
from world_engine.decisions import NpcReply
from world_engine.npc_performance import settle_npc_reply


@pytest.fixture
def endpoint(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE worlds(id TEXT PRIMARY KEY,current_time TEXT NOT NULL);
        CREATE TABLE characters(id TEXT PRIMARY KEY,world_id TEXT,is_player INTEGER,is_pov INTEGER);
        INSERT INTO worlds VALUES('world','2040-01-01T12:00:00+00:00');
        INSERT INTO characters VALUES('player','world',1,1);
        INSERT INTO characters VALUES('npc','world',0,0);
    """)
    calls = []
    class Database:
        @contextmanager
        def read(self):
            yield c
        @contextmanager
        def write(self):
            yield c
    def propose(_c, wid, player, npc, wonder, direction, key, at):
        calls.append(("propose", wid, player, npc, wonder, direction, key, at))
        return {"id": "proposal", "proposal_event_id": "event", "status": "pending"}
    def personal(op):
        def perform(_c, wid, player, proposal, at):
            calls.append((op, wid, player, proposal, at))
            return {"id": proposal, "status": "completed" if op == "traverse" else "cancelled"}
        return staticmethod(perform)
    monkeypatch.setattr(CrossLayerPassageService, "propose", staticmethod(propose))
    monkeypatch.setattr(CrossLayerPassageService, "player_traverse", personal("traverse"))
    monkeypatch.setattr(CrossLayerPassageService, "cancel", personal("cancel"))
    def approach(_c, wid, player, proposal, key, at):
        calls.append(("approach", wid, player, proposal, key, at))
        return {"id": proposal, "status": "pending", "approach_event_id": "event", "movement_id": "movement"}
    monkeypatch.setattr(CrossLayerPassageService, "player_approach", staticmethod(approach))
    monkeypatch.setattr(CrossLayerPassageService, "rows", staticmethod(lambda _c, wid, player, at: []))
    monkeypatch.setattr(CrossLayerPassageService, "options", staticmethod(lambda _c, wid, player, at: {"wonders": []}))
    app = FastAPI()
    app.include_router(build_cross_layer_router(Database()))
    yield TestClient(app), calls
    c.close()


def test_proposal_uses_actual_player_and_never_accepts_forged_actor(endpoint):
    client, calls = endpoint
    body = {"npc_id": "npc", "wonder_id": "wonder", "direction": "enter", "idempotency_key": "request"}
    url = "/api/worlds/world/cross-layer/proposals"
    assert client.post(url, json={**body, "player_id": "npc"}).status_code == 422
    result = client.post(url, json=body)
    assert result.status_code == 200 and result.json()["status"] == "pending"
    assert calls[0][:7] == ("propose", "world", "player", "npc", "wonder", "enter", "request")


@pytest.mark.parametrize("op", ["traverse", "cancel"])
def test_personal_action_cannot_supply_npc_actor(endpoint, op):
    client, calls = endpoint
    url = "/api/worlds/world/cross-layer/proposal/" + op
    assert client.post(url, json={"actor_id": "npc"}).status_code == 422
    assert client.post(url).status_code == 200
    assert calls[0][:4] == (op, "world", "player", "proposal")


def test_options_and_history_are_read_only_shapes(endpoint):
    client, calls = endpoint
    assert client.get("/api/worlds/world/cross-layer/options").json() == {"wonders": []}
    assert client.get("/api/worlds/world/cross-layer").json() == {"passages": []}
    assert not calls
    assert client.get("/api/worlds/missing/cross-layer/options").status_code == 404


def test_approach_api_requires_key_and_current_player_only(endpoint):
    client, calls = endpoint
    url = "/api/worlds/world/cross-layer/proposal/approach"
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"idempotency_key": "walk", "actor_id": "npc"}).status_code == 422
    result = client.post(url, json={"idempotency_key": "walk"})
    assert result.status_code == 200 and result.json()["movement_id"] == "movement"
    assert calls[0][:5] == ("approach", "world", "player", "proposal", "walk")


def test_actual_approach_api_starts_only_player_route_and_reuses_receipt(prepared):
    e = prepared
    from test_cross_layer_approach import offset
    with e["db"].write() as c:
        offset(c, e)
        before = c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (e["player"],)).fetchone()[:]
    app = FastAPI()
    app.include_router(build_cross_layer_router(e["db"]))
    client = TestClient(app)
    base = f"/api/worlds/{e['wid']}/cross-layer"
    proposal = client.post(base + "/proposals", json={"npc_id": e["first"], "wonder_id": e["city"]["id"],
        "direction": "enter", "idempotency_key": "actual-approach-invitation"}).json()
    assert proposal["status"] == "pending"
    payload = {"idempotency_key": "actual-approach-player"}
    url = base + f"/{proposal['id']}/approach"
    first = client.post(url, json=payload)
    assert first.status_code == 200 and first.json()["approach_status"] == "applied", first.text
    second = client.post(url, json=payload)
    assert second.status_code == 200 and second.json()["approach_status"] == "already"
    assert second.json()["movement_id"] == first.json()["movement_id"]
    with e["db"].read() as c:
        assert c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (e["player"],)).fetchone()[:] == before
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (first.json()["movement_id"],)).fetchone()[0] == "moving"
        assert not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (e["first"],)).fetchone()


def test_npc_approach_is_separate_structured_choice_not_passage(prepared):
    e = prepared
    from test_cross_layer_approach import offset, propose
    with e["db"].write() as c:
        offset(c, e)
        proposal = propose(c, e)
        source = talk(c, e)
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e["first"],)).fetchone()
        choice = NpcReply.model_validate({"reply": "我自己走近看看。", "action_intent":
            {"kind": "cross_layer", "target_id": proposal["id"], "operation": "approach"}})
        resolved = settle_npc_reply(c, npc=npc, player_id=e["player"], at=at(c,e), channel="in_person",
                                   reply=choice, source_event_id=source.event_id)
        assert resolved.action_status == "applied" and "尚未通过" in resolved.stage_direction
        assert c.execute("SELECT status FROM cross_layer_passages WHERE id=?", (proposal["id"],)).fetchone()[0] == "pending"
        assert c.execute("SELECT space_layer FROM characters WHERE id=?", (e["first"],)).fetchone()[0] == "ground"
        assert c.execute("SELECT status FROM character_movements WHERE character_id=? ORDER BY rowid DESC LIMIT 1", (e["first"],)).fetchone()[0] == "moving"


def test_actual_api_separates_proposal_npc_choice_and_player_pass(prepared):
    e = prepared
    app = FastAPI()
    app.include_router(build_cross_layer_router(e["db"]))
    client = TestClient(app)
    base = f"/api/worlds/{e['wid']}/cross-layer"
    choices = client.get(base + "/options")
    assert choices.status_code == 200
    assert any(w["id"] == e["city"]["id"] and any(p["id"] == e["first"] for p in w["people"])
               for w in choices.json()["wonders"])
    payload = {"npc_id": e["first"], "wonder_id": e["city"]["id"], "direction": "enter",
               "idempotency_key": "api-own-proposal"}
    created = client.post(base + "/proposals", json=payload)
    assert created.status_code == 200 and created.json()["status"] == "pending"
    pid = created.json()["id"]
    assert client.post(base + "/proposals", json=payload).json()["id"] == pid
    assert client.post(base + f"/{pid}/traverse").status_code == 409
    with e["db"].write() as c:
        outcome = talk(c, e)
        npc = c.execute("SELECT * FROM characters WHERE id=?", (e["first"],)).fetchone()
        choice = NpcReply.model_validate({"reply": "我愿意先通过。", "action_intent":
            {"kind": "cross_layer", "target_id": pid, "operation": "enter"}})
        resolved = settle_npc_reply(c, npc=npc, player_id=e["player"], at=at(c, e),
                                   channel="in_person", reply=choice, source_event_id=outcome.event_id)
        assert resolved.action_status == "applied"
        assert c.execute("SELECT space_layer FROM characters WHERE id=?", (e["player"],)).fetchone()[0] == "ground"
    completed = client.post(base + f"/{pid}/traverse")
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    assert client.post(base + f"/{pid}/traverse").json()["player_event_id"] == completed.json()["player_event_id"]
    with e["db"].read() as c:
        assert c.execute("SELECT space_layer FROM characters WHERE id=?", (e["player"],)).fetchone()[0] == e["city"]["space_layer"]
