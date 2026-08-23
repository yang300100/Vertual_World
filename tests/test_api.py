from __future__ import annotations

from fastapi.testclient import TestClient

from world_engine.api import create_app


def test_api_creates_reads_and_ticks_world(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        created = client.post(
            "/api/worlds",
            json={"name": "API世界", "minutes_per_tick": 90, "seed_demo": True},
        )
        assert health.status_code == 200
        assert created.status_code == 201

        world = created.json()
        world_id = world["world"]["id"]
        ticked = client.post(f"/api/worlds/{world_id}/tick")
        fetched = client.get(f"/api/worlds/{world_id}")
        events = client.get(f"/api/worlds/{world_id}/events")

    assert ticked.status_code == 200
    assert ticked.json()["current_version"] == 1
    assert fetched.status_code == 200
    assert fetched.json()["world"]["tick_count"] == 1
    assert events.status_code == 200
    assert len(events.json()) >= 4


def test_api_returns_404_for_missing_world(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/worlds/not-found")
    assert response.status_code == 404

