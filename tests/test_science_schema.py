"""哥哥，科研从真实契约开始；这些检查只使用临时数据库。"""

import importlib
import importlib.util
import sqlite3
from copy import deepcopy
from datetime import UTC

import pytest

from world_engine.repository import WorldRepository, to_iso


def module(name):
    assert importlib.util.find_spec("world_engine." + name), "尚无科研契约与会话功能"
    return importlib.import_module("world_engine." + name)


@pytest.fixture
def science_world(database):
    with database.write() as c:
        wid = WorldRepository().create_world(
            c, name="科研契约", minutes_per_tick=60, seed_demo=False, seed_maps=False
        )
        at = WorldRepository().get_snapshot(c, wid).world.current_time
    return database, wid, at


def declaration():
    return {
        "op": "declare",
        "name": "测试内候选",
        "problem": "检验材料性质",
        "claim": {
            "metric": "strength",
            "direction": "increase",
            "minimum_delta": 1,
            "conditions": {},
            "comparator_role": "control",
        },
        "method": {
            "inputs": {"sample": "material"},
            "parameters": {},
            "steps": [{"op": "store", "input": "sample", "hours": 1}],
        },
        "evidence_ids": [],
    }


def parse(command):
    return module("science_types").ScienceProgram.model_validate(
        {"reason": "测试真实边界", "commands": [command]}
    )


def test_wish_cannot_be_a_declaration():
    types = module("science_types")
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate(
            {
                "reason": "试验",
                "commands": [{"op": "declare", "name": "只写愿望", "method": "无限供能"}],
            }
        )


def test_valid_declaration_roundtrips_without_promoting_to_technology():
    body = declaration()
    assert parse(body).commands[0].model_dump()["method"]["steps"][0]["op"] == "store"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -1, 0, True])
def test_claim_rejects_invalid_difference(number):
    body = declaration()
    body["claim"]["minimum_delta"] = number
    types = module("science_types")
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


@pytest.mark.parametrize("op", ["python", "sql", "unlock", "spawn"])
def test_method_rejects_nonphysical_operations(op):
    body = declaration()
    body["method"]["steps"][0]["op"] = op
    types = module("science_types")
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


def test_method_rejects_forward_output_and_missing_role():
    types = module("science_types")
    for reference in ("$future:0", "missing"):
        body = declaration()
        body["method"]["steps"][0]["input"] = reference
        with pytest.raises(ValueError):
            types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


def test_method_limits_steps_and_validates_parameter_bounds():
    types = module("science_types")
    body = declaration()
    body["method"]["steps"] *= 25
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})
    body = declaration()
    body["method"]["parameters"] = {"temperature": {"minimum": 100, "maximum": 50, "unit": "C"}}
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


def test_experiment_requires_preexisting_prediction():
    types = module("science_types")
    with pytest.raises(ValueError, match="假说|预测"):
        types.ScienceProgram.model_validate(
            {
                "reason": "验证",
                "commands": [
                    {
                        "op": "experiment",
                        "batch_id": "real-batch",
                        "method": "strength",
                        "instrument_id": "tool",
                    }
                ],
            }
        )


def test_input_role_must_name_real_resource_type():
    types = module("science_types")
    body = declaration()
    body["method"]["inputs"] = {"sample": "imaginary"}
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


def test_undefined_parameter_and_unknown_fields_are_rejected():
    types = module("science_types")
    for changed in (
        {
            "method": {
                "inputs": {"sample": "material"},
                "parameters": {},
                "steps": [
                    {
                        "op": "heat",
                        "input": "sample",
                        "hours": 1,
                        "settings": {"temperature": "param:unknown"},
                    }
                ],
            }
        },
        {"assistant_solution": "禁止注入"},
    ):
        body = deepcopy(declaration())
        body.update(changed)
        with pytest.raises(ValueError):
            types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


def test_migration_reentrant_and_old_world_does_not_start_research(science_world):
    schema = module("science_schema")
    db, wid, _ = science_world
    with db.write() as c:
        schema.install_science_schema(c)
        schema.install_science_schema(c)
        assert c.execute("SELECT COUNT(*) FROM science_sessions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0
        assert c.execute("SELECT name FROM worlds WHERE id=?", (wid,)).fetchone()[0] == "科研契约"


def test_session_creation_is_persistent_and_never_seeds_technology(science_world):
    schema = module("science_schema")
    db, wid, at = science_world
    with db.write() as c:
        sid = schema.create_session(c, wid, at)
    with db.read() as c:
        row = c.execute("SELECT * FROM science_sessions WHERE id=?", (sid,)).fetchone()
        assert (row["status"], row["max_calls"], row["token_limit"], row["max_output_tokens"]) == (
            "prepared",
            12,
            200000,
            8192,
        )
        assert row["created_at"] == to_iso(at)
        assert c.execute("SELECT COUNT(*) FROM science_model_calls").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0


def test_cannot_reopen_stopped_world_to_reset_ledger(science_world):
    schema = module("science_schema")
    db, wid, at = science_world
    with db.write() as c:
        sid = schema.create_session(c, wid, at)
        c.execute("UPDATE science_sessions SET status='stopped' WHERE id=?", (sid,))
        with pytest.raises(ValueError, match="已有|恢复"):
            schema.create_session(c, wid, at)


@pytest.mark.parametrize(
    "options", [{"max_calls": True}, {"token_limit": -1}, {"max_output_tokens": 0}]
)
def test_invalid_budget_never_creates_session(science_world, options):
    schema = module("science_schema")
    db, wid, at = science_world
    with db.write() as c:
        with pytest.raises(ValueError):
            schema.create_session(c, wid, at, **options)
        assert c.execute("SELECT COUNT(*) FROM science_sessions").fetchone()[0] == 0


def test_session_world_and_budget_cannot_be_changed(science_world):
    schema = module("science_schema")
    db, wid, at = science_world
    with db.write() as c:
        sid = schema.create_session(c, wid, at)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE science_sessions SET token_limit=999999 WHERE id=?", (sid,))


def test_missing_world_cannot_start_session(database):
    schema = module("science_schema")
    from datetime import datetime

    with database.write() as c, pytest.raises(ValueError, match="世界"):
        schema.create_session(c, "missing", datetime.now(UTC))


def test_database_initialize_installs_science_tables(database):
    with database.read() as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "science_sessions",
        "technologies",
        "technology_knowledge",
        "science_model_calls",
        "science_claim_trials",
    } <= names


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_native_processing_settings_must_be_finite_numbers(value):
    types = module("science_types")
    with pytest.raises(ValueError):
        types.ScienceProgram.model_validate(
            {
                "reason": "处理实物",
                "commands": [
                    {
                        "op": "produce",
                        "operation": "heat",
                        "batch_id": "real",
                        "settings": {"temperature": value},
                    }
                ],
            }
        )


def test_assay_cannot_bind_material_as_instrument():
    types = module("science_types")
    body = declaration()
    body["method"]["steps"] = [
        {"op": "assay", "input": "sample", "metric": "strength", "instrument": "sample"}
    ]
    with pytest.raises(ValueError, match="量器|instrument"):
        types.ScienceProgram.model_validate({"reason": "验证", "commands": [body]})


def test_call_cannot_link_session_of_different_world(science_world):
    schema = module("science_schema")
    db, wid, at = science_world
    with db.write() as c:
        sid = schema.create_session(c, wid, at)
        other = WorldRepository().create_world(
            c, name="另一世界", minutes_per_tick=60, seed_demo=True, seed_maps=False
        )
        actor = c.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (other,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO science_model_calls(id,session_id,world_id,actor_id,fingerprint,"
                "reason,context_json,system_prompt,reserved_tokens,created_at) "
                "VALUES ('call',?,?,?,'fp','测试','{}','测试',100,?)",
                (sid, other, actor, to_iso(at)),
            )
