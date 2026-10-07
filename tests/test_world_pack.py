from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.world_pack import WorldPackError, load_world_pack
from world_engine.worker import WorldWorker


def make_pack(root, pack_id: str, place: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    (root / "knowledge").mkdir(exist_ok=True)
    (root / "assets" / "map.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (root / "knowledge" / "lore.md").write_text(f"# {place}\n这里是{place}。", encoding="utf-8")
    (root / "locations.json").write_text(json.dumps([{"key": "start", "name": place, "kind": "town", "longitude": 10, "latitude": 20}]), encoding="utf-8")
    (root / "characters.json").write_text(json.dumps([{"key": "guide", "name": "向导", "location": "start", "species": "wanderer", "traits": ["友善"]}]), encoding="utf-8")
    (root / "species.json").write_text(json.dumps([{"id": "wanderer", "display_name": "漫游者", "gestation_world_days": 100, "adult_age_world_years": 10, "compatible_species": ["wanderer"]}]), encoding="utf-8")
    (root / "maps.json").write_text(json.dumps([{"key": "world", "name": "地图", "asset_path": "map.svg", "width_pixels": 360, "height_pixels": 180}]), encoding="utf-8")
    (root / "world.json").write_text(json.dumps({"format_version": 1, "id": pack_id, "revision": 1, "name": place, "start_time": "2050-01-01T08:00:00+00:00", "locations": "locations.json", "characters": "characters.json", "maps": "maps.json", "species": "species.json", "knowledge": ["knowledge"], "assets_root": "assets", "map_layers": ["map.svg"], "player": {"species": "wanderer", "money": 7, "skills": ["口才"], "items": []}}), encoding="utf-8")


def test_world_pack_switch_restores_independent_save(settings, tmp_path) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    configured = replace(settings, world_pack_directory=root)
    with TestClient(create_app(configured)) as client:
        worlds = client.get("/api/worlds").json()
        assert len(worlds) == 1
        world_a = worlds[0]["id"]
        snapshot = client.get(f"/api/worlds/{world_a}").json()
        assert [place["name"] for place in snapshot["locations"]] == ["青城"]
        assert [person["name"] for person in snapshot["characters"]] == ["向导"]
        assert client.get("/api/world-map-layers").json()["files"] == ["map.svg"]
        assert client.get("/world-assets/map.svg").status_code == 200
        player = client.post(f"/api/worlds/{world_a}/player", json={"name": "旅人", "identity": "记录者", "location_id": snapshot["locations"][0]["id"]})
        assert player.status_code == 201, player.text
        player_state = next(person for person in player.json()["characters"] if person["name"] == "旅人")
        assert player_state["money"] == 7
        assert player_state["skills"] == ["口才"]
        from world_engine.database import Database
        runtime_db = Database(tmp_path / "world-saves" / "world-a" / settings.database_path.name)
        with runtime_db.read() as connection:
            assert [row["id"] for row in connection.execute("SELECT id FROM species_profiles")] == ["wanderer"]
            assert connection.execute("SELECT species FROM characters WHERE id=?", (player_state["id"],)).fetchone()["species"] == "wanderer"
            assert connection.execute("SELECT COUNT(*) FROM world_element_catalog WHERE world_id=?", (world_a,)).fetchone()[0] >= 2
    make_pack(root, "world-b", "赤港")
    with TestClient(create_app(configured)) as client:
        worlds = client.get("/api/worlds").json()
        assert len(worlds) == 1 and worlds[0]["id"] != world_a
        assert [place["name"] for place in client.get(f"/api/worlds/{worlds[0]['id']}").json()["locations"]] == ["赤港"]
    make_pack(root, "world-a", "青城")
    with TestClient(create_app(configured)) as client:
        assert [world["id"] for world in client.get("/api/worlds").json()] == [world_a]
        assert any(person["name"] == "旅人" for person in client.get(f"/api/worlds/{world_a}").json()["characters"])


def test_same_id_changed_content_does_not_open_save(settings, tmp_path, monkeypatch) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    configured = replace(settings, world_pack_directory=root)
    with TestClient(create_app(configured)) as client:
        assert len(client.get("/api/worlds").json()) == 1
    make_pack(root, "world-a", "赤港")
    monkeypatch.setattr("world_engine.api.Database.initialize", lambda self: (_ for _ in ()).throw(AssertionError("冲突前不得迁移旧存档")))
    with pytest.raises(WorldPackError, match="冲突|不兼容"):
        with TestClient(create_app(configured)):
            pass


def test_world_pack_rejects_escaping_file(tmp_path) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    manifest = json.loads((root / "world.json").read_text(encoding="utf-8"))
    manifest["locations"] = "../outside.json"
    (root / "world.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorldPackError, match="路径"):
        load_world_pack(root)


@pytest.mark.parametrize("change", [
    {"characters": "missing.json"},
    {"maps": "../escape.json"},
    {"locations": None},
])
def test_world_pack_reports_bad_manifest_paths(tmp_path, change) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    manifest = json.loads((root / "world.json").read_text(encoding="utf-8"))
    manifest.update(change)
    (root / "world.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorldPackError, match="路径"):
        load_world_pack(root)


def test_world_pack_reports_missing_required_file_field(tmp_path) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    manifest = json.loads((root / "world.json").read_text(encoding="utf-8"))
    del manifest["species"]
    (root / "world.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorldPackError, match="species"):
        load_world_pack(root)


def test_new_pack_can_use_a_filename_retired_only_in_noryia(settings, tmp_path) -> None:
    from world_engine.world_pack import resolve_pack_settings
    from world_engine.knowledge import WorldKnowledgeBase

    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    (root / "knowledge" / "17-history-of-three-regions.md").write_text("# 新世界历史\n这段历史属于青城。", encoding="utf-8")
    resolved, _pack = resolve_pack_settings(replace(settings, world_pack_directory=root))
    knowledge = WorldKnowledgeBase.from_paths(resolved.knowledge_paths, excluded_filenames=resolved.knowledge_excluded_filenames)
    assert knowledge.chunk_count == 2


def test_pack_can_seed_nested_locations(settings, tmp_path) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    locations = json.loads((root / "locations.json").read_text(encoding="utf-8"))
    locations.append({"key": "inn", "name": "旅店", "kind": "home", "longitude": 10.01, "latitude": 20, "parent": "start"})
    (root / "locations.json").write_text(json.dumps(locations), encoding="utf-8")
    with TestClient(create_app(replace(settings, world_pack_directory=root))) as client:
        world_id = client.get("/api/worlds").json()[0]["id"]
        by_name = {place["name"]: place for place in client.get(f"/api/worlds/{world_id}").json()["locations"]}
        assert by_name["旅店"]["parent_location_id"] == by_name["青城"]["id"]


def test_worker_uses_active_pack_save(settings, tmp_path) -> None:
    root = tmp_path / "docs"
    make_pack(root, "world-a", "青城")
    configured = replace(settings, world_pack_directory=root)
    with TestClient(create_app(configured)) as client:
        world_id = client.get("/api/worlds").json()[0]["id"]
    worker = WorldWorker(configured)
    try:
        assert worker.database.path == tmp_path / "world-saves" / "world-a" / settings.database_path.name
        assert worker.run_once(elapsed_seconds=0) == 1
    finally:
        worker.engine.close()
    with TestClient(create_app(configured)) as client:
        assert client.get("/api/worlds").json()[0]["id"] == world_id


def test_noryia_pack_creates_and_reopens_from_files(settings) -> None:
    root = Path(__file__).resolve().parents[1] / "docs"
    configured = replace(settings, world_pack_directory=root)
    with TestClient(create_app(configured)) as client:
        world_id = client.get("/api/worlds").json()[0]["id"]
        snapshot = client.get(f"/api/worlds/{world_id}").json()
        assert len(snapshot["locations"]) == 618
        assert len(snapshot["maps"]) > 1
    with TestClient(create_app(configured)) as client:
        assert client.get("/api/worlds").json()[0]["id"] == world_id


def test_existing_noryia_database_is_kept_as_its_save(settings) -> None:
    from world_engine.database import Database
    from world_engine.repository import WorldRepository

    database = Database(settings.database_path)
    database.initialize()
    with database.write() as connection:
        old_id = WorldRepository().create_world(connection, name="旧诺里亚存档", minutes_per_tick=60)
    configured = replace(settings, world_pack_directory=Path(__file__).resolve().parents[1] / "docs")
    with TestClient(create_app(configured)) as client:
        assert [world["id"] for world in client.get("/api/worlds").json()] == [old_id]
        assert len(client.get(f"/api/worlds/{old_id}").json()["locations"]) == 3
