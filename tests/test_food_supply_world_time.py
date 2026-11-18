"""哥哥，新世界的食物再生从真实世界时刻开始，不用现实日期补造多年资源。"""

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from test_lived_world import LivingModel
from world_engine.api import create_app
from world_engine.database import Database
from world_engine.economy import EconomyService
from world_engine.food_supply import seed_food_supply
from world_engine.repository import from_iso


def test_new_food_supply_uses_world_time_and_no_premature_regeneration(settings, monkeypatch):
    model = LivingModel()
    monkeypatch.setattr("world_engine.engine.build_decision_provider", lambda _: model)
    monkeypatch.setattr("world_engine.engine.build_agent_model_backend", lambda _: model)
    db = Database(settings.database_path)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/worlds", json={"name": "隔离食物时间验收", "time_scale": 1})
        assert response.status_code == 201, response.text
        wid = response.json()["world"]["id"]
        with db.write() as c:
            current = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()[0]
            profile = c.execute("SELECT * FROM world_item_profiles WHERE world_id=? AND resource_key='food' AND resource_location_id IS NULL", (wid,)).fetchone()
            assert profile and profile["last_growth_world_time"] == current
            event = c.execute("SELECT * FROM world_events WHERE id=(SELECT source_event_id FROM element_registration_requests WHERE id=?)", (profile["registration_id"],)).fetchone()
            assert event["event_type"] == "world.food_supply_initialized" and event["occurred_at"] == current
            before = {r["id"]: json.loads(r["resources_json"] or "{}") for r in c.execute("SELECT id,resources_json FROM locations WHERE world_id=?", (wid,))}
            EconomyService.tick(c, wid, from_iso(current) + timedelta(hours=1))
            after = {r["id"]: json.loads(r["resources_json"] or "{}") for r in c.execute("SELECT id,resources_json FROM locations WHERE world_id=?", (wid,))}
            assert after == before
            assert c.execute("SELECT COUNT(*) FROM world_events WHERE world_id=? AND event_type='world.resource_regenerated'", (wid,)).fetchone()[0] == 0
            seed_food_supply(c, wid)
            assert c.execute("SELECT last_growth_world_time FROM world_item_profiles WHERE item_type_id=?", (profile["item_type_id"],)).fetchone()[0] == current
