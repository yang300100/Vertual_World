"""哥哥，跨轮账本来自已停止的真实记录，测试仍全部离线。"""

import importlib
import json

import pytest

from world_engine.database import Database
from world_engine.science_freeze import FrozenScienceDatabase, backup_world, freeze_session

pytest_plugins = ["science_fixtures"]


@pytest.fixture
def prior(lab, tmp_path):
    from world_engine.science_schema import create_session
    from world_engine.science_workshop import seed_workshop

    db, wid, actors, _, _, at = lab
    output = tmp_path / "prior-run"
    output.mkdir()
    backup_world(db.path, output / "source.db")
    code = tmp_path / "prior-code.py"
    code.write_text("neutral", encoding="utf-8")
    with db.write() as c:
        sid = create_session(c, wid, at)
        seed_workshop(c, wid, at, seed=7)
        freeze_session(c, sid, source_paths=(code,), world_paths=(code,))
    guarded = FrozenScienceDatabase(db.path, session_id=sid)
    with guarded.write() as c:
        c.execute(
            "INSERT INTO science_model_calls(id,session_id,world_id,actor_id,fingerprint,status,"
            "reason,context_json,system_prompt,reserved_tokens,accounted_tokens,input_tokens,"
            "output_tokens,created_at) VALUES ('old',?,?,?,'old','failed','length','{}','',"
            "30000,21304,13112,8192,?)",
            (sid, wid, actors[0], at.isoformat()),
        )
        c.execute(
            "UPDATE science_sessions SET status='stopped',stop_reason='length' WHERE id=?", (sid,)
        )
    backup_world(db.path, output / "world.db")
    (output / "run.json").write_text(
        json.dumps({"session_id": sid, "world_id": wid}), encoding="utf-8"
    )
    return output, wid, sid, actors[0], at


def lineage(paths, wid):
    return importlib.import_module("world_engine.science_budget").budget_lineage(paths, wid)


def test_prior_usage_deducted_and_read_only(prior):
    output, wid, _, _, _ = prior
    original = (output / "world.db").read_bytes()
    result = lineage((output,), wid)
    assert result["total_limits"] == {"max_calls": 12, "token_limit": 200000}
    assert result["remaining"] == {"max_calls": 11, "token_limit": 178696}
    assert result["prior_runs"][0]["calls"] == 1
    assert result["prior_runs"][0]["accounted_tokens"] == 21304
    assert (output / "world.db").read_bytes() == original


def test_duplicates_and_other_world_refused(prior):
    output, wid, _, _, _ = prior
    with pytest.raises(ValueError, match="重复"):
        lineage((output, output), wid)
    with pytest.raises(ValueError, match="世界"):
        lineage((output,), "another-world")


@pytest.mark.parametrize("change", ["active", "tamper", "exhausted"])
def test_unsettled_changed_or_exhausted_prior_refused(prior, change):
    output, wid, sid, _, _ = prior
    guarded = FrozenScienceDatabase(output / "world.db", session_id=sid)
    if change == "tamper":
        with Database(output / "world.db", legacy_world_defaults=False).write() as c:
            c.execute("UPDATE science_model_calls SET accounted_tokens=1")
    else:
        with guarded.write() as c:
            if change == "active":
                c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
            else:
                c.execute("UPDATE science_model_calls SET accounted_tokens=200000")
    with pytest.raises(ValueError):
        lineage((output,), wid)


def test_unknown_prior_keeps_conservative_cost(prior):
    output, wid, sid, _, _ = prior
    with FrozenScienceDatabase(output / "world.db", session_id=sid).write() as c:
        c.execute(
            "UPDATE science_model_calls SET status='unknown',input_tokens=NULL,"
            "output_tokens=NULL,accounted_tokens=reserved_tokens"
        )
    result = lineage((output,), wid)
    assert result["remaining"]["token_limit"] == 170000


def test_later_run_cannot_omit_ancestor_cost(prior, settings, tmp_path):
    from scripts.demo_science import prepare

    output, wid, _, _, _ = prior
    later = tmp_path / "later-run"
    prepare(output / "source.db", wid, later, settings, {}, prior_runs=(output,))
    sid = json.loads((later / "run.json").read_text(encoding="utf-8"))["session_id"]
    with FrozenScienceDatabase(later / "world.db", session_id=sid).write() as c:
        c.execute("UPDATE science_sessions SET status='stopped' WHERE id=?", (sid,))
    with pytest.raises(ValueError, match="祖先"):
        lineage((later,), wid)
    assert lineage((output, later), wid)["remaining"] == {
        "max_calls": 11,
        "token_limit": 178696,
    }


def test_prepare_freezes_remaining_and_rechecks_prior_before_engine(
    prior, settings, tmp_path, monkeypatch
):
    from scripts.demo_science import prepare, run

    output, wid, sid, _, _ = prior
    new = tmp_path / "new-prepared"
    frozen = prepare(
        output / "source.db",
        wid,
        new,
        settings,
        {"offline_passed": True, "independent_review_passed": True},
        prior_runs=(output,),
    )
    assert frozen["budget"]["max_calls"] == 11
    assert frozen["budget"]["token_limit"] == 178696
    assert frozen["verification"]["budget_lineage"] == lineage((output,), wid)
    with FrozenScienceDatabase(output / "world.db", session_id=sid).write() as c:
        c.execute("UPDATE science_model_calls SET accounted_tokens=22000")

    def forbidden(*args, **kwargs):
        pytest.fail("哥哥，费用来源改变须在构造引擎前拒绝")

    monkeypatch.setattr("scripts.demo_science.WorldEngine", forbidden)
    with pytest.raises(ValueError, match="跨轮"):
        run(new, settings)
