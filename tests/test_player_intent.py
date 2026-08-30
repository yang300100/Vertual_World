from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.domain import ActionType
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world


def test_submit_intent_without_player_raises(database, settings) -> None:
    """没有玩家角色时，提交意图应报错。"""
    world_id = create_iserra_world(database)
    engine = WorldEngine(database, settings)
    with pytest.raises(ValueError):
        engine.submit_player_intent(world_id, "去东澜港看看")


def test_submit_intent_travel_settles(database, settings) -> None:
    """玩家意图“去东澜港”应转成一次 TRAVEL 行动并落库。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        start_location = next(
            location.id for location in snapshot.locations if location.name == "澜誓城"
        )
    with database.write() as connection:
        repo.create_player_character(
            connection,
            world_id=world_id,
            name="旅人",
            identity="远行学者",
            location_id=start_location,
            traits=["谨慎"],
            goal="记录伊瑟拉",
        )

    engine = WorldEngine(database, settings)
    result = engine.submit_player_intent(world_id, "我要去东澜港看看")

    assert result.outcome.accepted is True
    assert result.outcome.action is ActionType.TRAVEL
    assert result.provider == "rules"
    assert result.current_version == result.previous_version + 1

    with database.read() as connection:
        events = repo.list_events(connection, world_id, limit=20)
    assert any(event["event_type"] == "action.travel" for event in events)


def test_api_player_act_endpoint(settings) -> None:
    """POST /player/act 应返回一次成功结算的行动。"""
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "玩家世界", "seed_demo": True}
        ).json()
        world_id = created["world"]["id"]
        start_location = next(
            loc["id"] for loc in created["locations"] if loc["name"] != "河畔住宅"
        )

        created_player = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "阿澈",
                "identity": "旅人",
                "location_id": start_location,
                "traits": [],
                "goal": "记录世界",
            },
        )
        assert created_player.status_code == 201

        act = client.post(
            f"/api/worlds/{world_id}/player/act",
            json={"intent": "我要去河畔住宅看看"},
        )
        assert act.status_code == 201
        body = act.json()
        assert body["outcome"]["accepted"] is True
        assert body["outcome"]["action"] == "travel"
