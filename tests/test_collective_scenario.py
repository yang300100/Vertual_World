"""哥哥，场景只给灾前日常资源和一次灰雨，不能预写解决过程。"""

import json

import pytest

from world_engine.materials import MaterialService as M
from world_engine.polities import current_polities


def scenario():
    from scripts import demo_collective_disaster

    return demo_collective_disaster


def test_runtime_freeze_records_python_sqlite_and_actual_dependency_versions():
    import sqlite3
    import sys

    runtime = scenario().runtime_manifest()
    assert runtime["python"] == sys.version
    assert runtime["sqlite"] == sqlite3.sqlite_version
    assert runtime["packages"]["pydantic"]
    assert runtime == scenario().runtime_manifest()


def test_local_residents_can_discover_finite_natural_sources_without_hidden_state(database):
    wid, at = scenario().build_baseline(database)
    from world_engine.collective_research import CollectiveResearchService

    with database.read() as c:
        item = c.execute(
            "SELECT * FROM item_instances WHERE world_id=? AND container_type='natural_resource' LIMIT 1",
            (wid,),
        ).fetchone()
        view = CollectiveResearchService.context(c, item["owner_character_id"], at)
        assert any(source["id"] == item["id"] for source in view["natural_sources"])
        encoded = json.dumps(view["natural_sources"])
        assert "mobile" not in encoded and "viability" not in encoded


def test_baseline_covers_all_polities_without_disaster_programs(database):
    s = scenario()
    wid, at = s.build_baseline(database)
    with database.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_regions WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == len(current_polities())
            == 21
        )
        assert (
            c.execute("SELECT COUNT(*) FROM collective_people WHERE world_id=?", (wid,)).fetchone()[
                0
            ]
            == 63
        )
        for table in (
            "collective_programs",
            "collective_model_calls",
            "collective_hypotheses",
            "collective_proposals",
            "collective_observations",
        ):
            assert c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        assert M.audit(c, wid)["balanced"]
        assert all(
            salt == 0
            for (salt,) in c.execute(
                "SELECT json_extract(b.state_json,'$.mobile') FROM material_batches b JOIN item_instances i ON i.id=b.item_id WHERE b.world_id=? AND i.container_type!='world_atmosphere'",
                (wid,),
            )
        )


def test_one_finite_injection_preserves_material_origin_and_only_gives_local_sight(database):
    s = scenario()
    wid, at = s.build_baseline(database)
    with database.read() as c:
        before = M.audit(c, wid)
    s.inject_initial_disaster(database, wid, at)
    with database.read() as c:
        after = M.audit(c, wid)
        assert after["balanced"]
        assert before["origins"] == after["origins"]
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='eyewitness'"
            ).fetchone()[0]
            == 63
        )
        for row in c.execute(
            "SELECT o.content_json,a.current_location_id FROM collective_observations o JOIN characters a ON a.id=o.character_id"
        ):
            body = json.loads(row["content_json"])
            assert body["location_id"] == row["current_location_id"]
            assert "mobile" not in body and "mechanism" not in body
        assert c.execute("SELECT COUNT(*) FROM collective_programs").fetchone()[0] == 0
    with pytest.raises(ValueError, match="一次"):
        s.inject_initial_disaster(database, wid, at)


def test_scenario_real_loads_fit_initial_carrying_limits_and_food_is_finite(database):
    s = scenario()
    wid, at = s.build_baseline(database)
    with database.read() as c:
        for actor in c.execute("SELECT * FROM collective_people WHERE world_id=?", (wid,)):
            limit = c.execute(
                "SELECT max_load FROM collective_carry_limits WHERE actor_id=?",
                (actor["character_id"],),
            ).fetchone()[0]
            load = 0
            for item in c.execute(
                "SELECT i.*,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE container_type='character_inventory' AND container_id=?",
                (actor["character_id"],),
            ):
                state = M.state(c, item["id"])
                load += (
                    (state.mass + state.water) if state else item["quantity"] * item["slot_size"]
                )
            assert load <= limit
        assert (
            c.execute(
                "SELECT COUNT(*) FROM item_instances WHERE container_type='natural_resource'"
            ).fetchone()[0]
            > 0
        )
        assert c.execute("SELECT COUNT(*) FROM collective_route_limits").fetchone()[0] > 0


@pytest.mark.parametrize(
    "options,output_tokens,timeout",
    [({}, 131072, 600), ({"max_output_tokens": 65536, "timeout_seconds": 300}, 65536, 300)],
)
def test_init_only_exports_two_separate_frozen_databases_without_model(
    tmp_path, monkeypatch, options, output_tokens, timeout
):
    import world_engine.engine as engine_module
    from world_engine.engine import WorldEngine

    captured = []

    def capture_engine(db, settings):
        captured.append(settings)
        return WorldEngine(db, settings)

    monkeypatch.setattr(engine_module, "WorldEngine", capture_engine)
    s = scenario()
    output = s.run_scenario(tmp_path / "initial", init_only=True, **options)
    freeze = json.loads((output / "freeze.json").read_text(encoding="utf-8"))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert freeze["limits"] == {
        "calls": 120,
        "tokens": 1500000,
        "output_tokens": output_tokens,
        "timeout_seconds": timeout,
    }
    assert captured[0].deepseek_max_output_tokens == output_tokens
    assert captured[0].collective_max_output_tokens == output_tokens
    assert captured[0].deepseek_timeout_seconds == timeout
    assert freeze["time_policy"]["max_heartbeat_hours"] == 1
    assert (output / "baseline-world.db").is_file() and (output / "world.db").is_file()
    assert report["usage"]["calls"] == 0 and report["goal_achieved"] is False


def test_frozen_manifest_detects_source_mutation(tmp_path):
    s = scenario()
    with pytest.raises(RuntimeError, match="冻结"):
        s.frozen_check({"world_engine/material_physics.py": "wrong"})
