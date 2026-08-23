from __future__ import annotations

import json
from dataclasses import replace

import pytest

from world_engine.decisions import (
    DecisionProviderError,
    DeepSeekDecisionProvider,
    build_decision_provider,
)
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository


class FakeResponse:
    def __init__(self, content: str, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": self.content}}]}


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def post(self, path: str, json: dict[str, object]) -> FakeResponse:
        self.requests.append((path, json))
        return self.response

    def close(self) -> None:
        self.closed = True


def _create_snapshot(database):
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="模型测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        return repository.get_snapshot(connection, world_id)


def test_deepseek_provider_parses_fenced_structured_actions(database, settings) -> None:
    snapshot = _create_snapshot(database)
    characters = snapshot.characters[:2]
    response_payload = {
        "decisions": [
            {
                "actor_id": character.id,
                "action": "idle",
                "reason": "先观察当前环境。",
                "target_id": None,
                "destination_id": None,
                "metadata": {},
            }
            for character in characters
        ]
    }
    fake_client = FakeClient(
        FakeResponse(f"```json\n{json.dumps(response_payload, ensure_ascii=False)}\n```")
    )
    deepseek_settings = replace(
        settings,
        decision_provider="deepseek",
        deepseek_api_key="test-secret",
        deepseek_model="test-model",
    )
    provider = DeepSeekDecisionProvider(deepseek_settings, client=fake_client)

    proposals = provider.propose(snapshot, characters)
    provider.close()

    assert [item.actor_id for item in proposals] == [item.id for item in characters]
    assert fake_client.requests[0][0] == "chat/completions"
    assert fake_client.requests[0][1]["model"] == "test-model"
    assert "test-secret" not in json.dumps(fake_client.requests[0][1], ensure_ascii=False)
    assert fake_client.closed is True


def test_deepseek_provider_rejects_non_json_content(settings) -> None:
    deepseek_settings = replace(
        settings,
        decision_provider="deepseek",
        deepseek_api_key="test-secret",
    )
    provider = DeepSeekDecisionProvider(
        deepseek_settings, client=FakeClient(FakeResponse("这不是JSON"))
    )
    with pytest.raises(DecisionProviderError):
        provider._parse_decisions("这不是JSON")


def test_deepseek_configuration_requires_key(settings) -> None:
    deepseek_settings = replace(
        settings,
        decision_provider="deepseek",
        deepseek_api_key=None,
    )
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        build_decision_provider(deepseek_settings)


def test_provider_failure_falls_back_to_rules(database, settings) -> None:
    snapshot = _create_snapshot(database)

    class FailingProvider:
        name = "deepseek"

        def propose(self, snapshot, characters):
            raise DecisionProviderError("模拟上游故障")

    result = WorldEngine(database, settings, decision_provider=FailingProvider()).tick(
        snapshot.world.id
    )
    with database.read() as connection:
        events = WorldRepository().list_events(connection, snapshot.world.id)
    tick_event = next(item for item in events if item["event_type"] == "world.tick")

    assert result.current_version == 1
    assert len(result.outcomes) == 3
    assert tick_event["payload"]["provider"] == "rules"


def test_secret_is_hidden_from_settings_repr(settings) -> None:
    deepseek_settings = replace(settings, deepseek_api_key="test-secret")
    assert "test-secret" not in repr(deepseek_settings)

