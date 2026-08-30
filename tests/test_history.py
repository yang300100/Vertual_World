from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository


def _create_world(database) -> str:
    with database.write() as connection:
        return WorldRepository().create_world(
            connection,
            name="历史测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )


def test_tick_automatically_writes_markdown_jsonl_and_per_tick_logs(
    database, settings, tmp_path
) -> None:
    history_directory = tmp_path / "history"
    history_settings = replace(
        settings,
        history_logging_enabled=True,
        history_directory=history_directory,
    )
    world_id = _create_world(database)
    engine = WorldEngine(database, history_settings)

    engine.tick(world_id)
    engine.tick(world_id)

    world_directory = history_directory / world_id
    markdown = (world_directory / "history.md").read_text(encoding="utf-8")
    jsonl_lines = (world_directory / "history.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    tick_files = sorted((world_directory / "ticks").glob("*.json"))

    assert "# 历史测试世界 · 世界运行历史" in markdown
    assert "## 第 1 轮" in markdown
    assert "## 第 2 轮" in markdown
    assert "行动理由" in markdown
    assert len(jsonl_lines) == 6
    assert len(tick_files) == 2
    assert json.loads(jsonl_lines[-1])["event_type"] == "world.adjudication"
    assert json.loads(tick_files[-1].read_text(encoding="utf-8"))["sequence"] == 2


def test_existing_database_history_can_be_backfilled(database, settings, tmp_path) -> None:
    world_id = _create_world(database)
    WorldEngine(database, settings).tick(world_id)
    history_settings = replace(
        settings,
        history_logging_enabled=True,
        history_directory=tmp_path / "backfilled-history",
    )

    result = WorldEngine(database, history_settings).sync_history(world_id)

    assert result.tick_count == 1
    assert result.event_count == 3
    assert result.tick_files == 1


def test_history_export_failure_does_not_rollback_completed_tick(
    database, settings, tmp_path
) -> None:
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("占用路径", encoding="utf-8")
    history_settings = replace(
        settings,
        history_logging_enabled=True,
        history_directory=blocked_path,
    )
    world_id = _create_world(database)

    result = WorldEngine(database, history_settings).tick(world_id)

    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    assert result.current_version == 1
    assert snapshot.world.tick_count == 1


def test_history_sync_api_returns_generated_log_paths(settings, tmp_path) -> None:
    history_settings = replace(
        settings,
        history_logging_enabled=True,
        history_directory=tmp_path / "api-history",
    )
    app = create_app(history_settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "日志API世界"}).json()
        world_id = created["world"]["id"]
        client.post(f"/api/worlds/{world_id}/tick")
        response = client.post(f"/api/worlds/{world_id}/history/sync")

    assert response.status_code == 200
    assert response.json()["tick_count"] == 1
    assert response.json()["event_count"] == 3


def test_heartbeat_and_character_state_logs_are_separate(database, settings, tmp_path) -> None:
    history_directory = tmp_path / "separate-state-history"
    history_settings = replace(
        settings,
        history_logging_enabled=True,
        history_directory=history_directory,
    )
    world_id = _create_world(database)
    engine = WorldEngine(database, history_settings)

    heartbeat = engine.heartbeat(world_id, elapsed_seconds=3600)
    export = engine.sync_history(world_id)

    world_directory = history_directory / world_id
    heartbeat_lines = (world_directory / "heartbeats.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    state_lines = (world_directory / "state_updates.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    character_files = list((world_directory / "characters").glob("*.state.jsonl"))
    state_manifest = json.loads(
        (world_directory / "state_manifest.json").read_text(encoding="utf-8")
    )

    assert heartbeat.adjudication is None
    assert export.heartbeat_count == 1
    assert export.state_update_count == 3
    assert len(heartbeat_lines) == 1
    assert len(state_lines) == 3
    assert len(character_files) == 3
    assert state_manifest["heartbeat_count"] == 1
    assert state_manifest["state_update_count"] == 3


def test_time_scale_change_is_recorded_in_world_history(database, settings, tmp_path) -> None:
    history_directory = tmp_path / "clock-history"
    history_settings = replace(
        settings,
        history_logging_enabled=True,
        history_directory=history_directory,
    )
    world_id = _create_world(database)
    engine = WorldEngine(database, history_settings)

    result = engine.set_time_scale(world_id, 2.0, operator="main_view")

    markdown = (history_directory / world_id / "history.md").read_text(
        encoding="utf-8"
    )
    assert result.old_time_scale == 1.0
    assert result.new_time_scale == 2.0
    assert "时间比例调整" in markdown
    assert "世界时间比例由1.0调整为2.0" in markdown
