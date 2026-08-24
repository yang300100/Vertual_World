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
        assert health.json()["service"] == "virtual-world-core"
        assert health.json()["server_time"].endswith("+00:00")
        assert health.json()["knowledge"] == {"enabled": True, "loaded_chunks": 0}
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


def test_api_exposes_clock_heartbeat_and_adjudication_controls(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "时钟API世界"}).json()
        world_id = created["world"]["id"]
        actor_id = created["characters"][0]["id"]

        speed = client.patch(
            f"/api/worlds/{world_id}/clock",
            json={"time_scale": 2.0, "operator": "test"},
        )
        heartbeat = client.post(f"/api/worlds/{world_id}/heartbeat")
        adjudication = client.post(
            f"/api/worlds/{world_id}/adjudicate",
            json={
                "trigger": "player_intervention",
                "character_ids": [actor_id],
            },
        )
        runs = client.get(f"/api/worlds/{world_id}/adjudications")

    assert speed.status_code == 200
    assert speed.json()["new_time_scale"] == 2.0
    assert heartbeat.status_code == 200
    assert heartbeat.json()["time_scale"] == 2.0
    assert adjudication.status_code == 200
    assert adjudication.json()["trigger"] == "player_intervention"
    assert len(adjudication.json()["outcomes"]) == 1
    assert runs.status_code == 200
    assert len(runs.json()) == 1


def test_frontend_assets_are_served_by_fastapi(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        page = client.get("/ui/")
        styles = client.get("/ui/styles.css")
        script = client.get("/ui/app.js")
        favicon = client.get("/ui/favicon.svg")

    assert root.status_code == 307
    assert root.headers["location"] == "/ui/"
    assert page.status_code == 200
    assert "虚拟世界控制台" in page.text
    assert 'id="heartbeat-button"' in page.text
    assert 'id="character-grid"' in page.text
    assert 'id="worker-status"' in page.text
    assert styles.status_code == 200
    assert "--green-strong" in styles.text
    assert script.status_code == 200
    assert "/api/worlds/${state.worldId}/heartbeat" in script.text
    assert "function updateLiveClock()" in script.text
    assert favicon.status_code == 200
    assert "image/svg+xml" in favicon.headers["content-type"]
