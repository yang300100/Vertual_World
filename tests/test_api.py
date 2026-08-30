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
    assert len(events.json()) >= 3


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


def test_api_creates_a_dedicated_player_character(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "玩家世界"}).json()
        world_id = created["world"]["id"]
        location_id = created["locations"][0]["id"]
        response = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "阿澈",
                "identity": "远行学者",
                "location_id": location_id,
                "traits": ["谨慎", "好奇"],
                "goal": "记录陌生的遗迹",
            },
        )
        duplicate = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "第二位玩家",
                "identity": "旅人",
                "location_id": location_id,
            },
        )

    assert response.status_code == 201
    player_characters = [
        item for item in response.json()["characters"] if item["is_player"]
    ]
    assert len(player_characters) == 1
    assert player_characters[0]["name"] == "阿澈"
    assert player_characters[0]["is_pov"] is True
    assert duplicate.status_code == 409


def test_frontend_assets_are_served_by_fastapi(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        page = client.get("/ui/")
        styles = client.get("/ui/styles.css")
        script = client.get("/ui/app.js")
        favicon = client.get("/ui/favicon.svg")
        world_map = client.get("/world-assets/planet-master.png")

    assert root.status_code == 307
    assert root.headers["location"] == "/ui/"
    assert page.status_code == 200
    assert "伊瑟拉 · 旅人之书" in page.text
    assert 'id="player-name"' in page.text
    assert 'id="player-create-form"' in page.text
    assert 'id="player-intent-form"' in page.text
    assert 'id="location-map"' in page.text
    assert 'id="room-map"' in page.text
    assert 'id="room-notebook"' in page.text
    assert 'id="map-camera"' in page.text
    assert 'id="map-context-name"' in page.text
    assert 'id="room-log"' in page.text
    assert 'id="player-transport"' in page.text
    assert 'id="nearby-characters"' in page.text
    assert 'id="heartbeat-button"' in page.text
    assert 'id="character-grid"' in page.text
    assert 'id="worker-status"' in page.text
    assert styles.status_code == 200
    assert "--copper-bright" in styles.text
    assert "[hidden]" in styles.text
    assert script.status_code == 200
    assert "/api/worlds/${state.worldId}/heartbeat" in script.text
    assert "function updateLiveClock()" in script.text
    assert "function runLiveFrame()" in script.text
    assert "function renderPlayerExperience" in script.text
    assert "function renderRelationshipGraph" in script.text
    assert "function zoomMap" in script.text
    assert "function updateMapPointScreenPositions" in script.text
    assert "function selectDisplayMap" in script.text
    assert "mapContainsForDetailActivation(map, coordinate, nextScale)" in script.text
    assert "resolveZoomDetailMap(nextScale, focusCoordinate, { x: rect.left + focusX" in script.text
    assert "DETAIL_MAP_MIN_SCALE = 16" in script.text
    assert "DETAIL_MAP_ACTIVATION_PADDING_PX = 12" in script.text
    assert "DETAIL_MARKER_ACTIVATION_RADIUS_PX = 72" in script.text
    assert "function detailMapNearScreenPoint" in script.text
    assert "function enterDetailMap" in script.text
    assert "pointercancel" in script.text
    assert "activation_state" in script.text
    assert "/player/move" in script.text
    assert 'localStorage.getItem("iserra.notebook")' in script.text
    assert "virtual-world-intent:" in script.text
    assert favicon.status_code == 200
    assert "image/svg+xml" in favicon.headers["content-type"]
    assert world_map.status_code == 200
    assert world_map.headers["content-type"] == "image/png"
    assert "aspect-ratio: var(--map-aspect" in styles.text
