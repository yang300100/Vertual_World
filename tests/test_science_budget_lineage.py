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


def test_explicit_new_authorization_keeps_legacy_limits_and_prevents_reset(tmp_path, monkeypatch):
    from world_engine import science_budget as budget

    monkeypatch.setattr(budget, "AUTHORIZATION_DIRECTORY", tmp_path / "authorizations")
    authorization = {"id": "new-user-budget", "max_calls": 60, "token_limit": 600000}
    result = budget.budget_lineage((), "world", authorization=authorization)
    assert result["remaining"] == {"max_calls": 60, "token_limit": 600000}
    assert budget.budget_lineage((), "world")["total_limits"] == {
        "max_calls": 12,
        "token_limit": 200000,
    }
    output = tmp_path / "fresh"
    budget.register_allocation(authorization, output, ())
    assert (
        budget.budget_lineage((), "world", authorization=authorization, current_run=output)
        == result
    )
    with pytest.raises(ValueError, match="已登记账目"):
        budget.budget_lineage((), "world", authorization=authorization)
    with pytest.raises(ValueError, match="已登记账目|不一致"):
        budget.budget_lineage(
            (), "world", authorization=authorization | {"max_calls": 61}, current_run=output
        )


def test_old_budget_cannot_be_relabelled_as_new_authorization(prior, tmp_path, monkeypatch):
    from world_engine import science_budget as budget

    monkeypatch.setattr(budget, "AUTHORIZATION_DIRECTORY", tmp_path / "authorizations")
    output, wid, _, _, _ = prior
    with pytest.raises(ValueError, match="旧授权"):
        budget.budget_lineage(
            (output,), wid, authorization={"id": "new", "max_calls": 60, "token_limit": 600000}
        )


def test_new_prepare_starts_at_zero_and_freezes_finite_rations(
    lab, settings, tmp_path, monkeypatch
):
    from scripts.demo_science import prepare
    from world_engine import science_budget as budget

    monkeypatch.setattr(budget, "AUTHORIZATION_DIRECTORY", tmp_path / "authorizations")
    db, wid, actors, _, _, at = lab
    output = tmp_path / "new-zero-run"
    authorization = {"id": "zero-test", "max_calls": 60, "token_limit": 600000}
    with db.read() as c:
        before = c.execute(
            "SELECT health,energy,satiety FROM characters WHERE id=?", (actors[0],)
        ).fetchone()
    frozen = prepare(
        db.path,
        wid,
        output,
        settings,
        {
            "budget_authorization": authorization,
            "require_zero_technologies": True,
            "initial_ration_quantity": 40,
        },
    )
    assert frozen["budget"]["max_calls"] == 60
    assert frozen["verification"]["initial_technologies"] == 0
    with Database(output / "world.db", legacy_world_defaults=False).read() as c:
        after = c.execute(
            "SELECT health,energy,satiety FROM characters WHERE id=?", (actors[0],)
        ).fetchone()
        assert list(before) == list(after)
        assert c.execute("SELECT count(*) FROM science_model_calls").fetchone()[0] == 0
        for iid in frozen["verification"]["initial_rations"].values():
            assert (
                c.execute("SELECT quantity FROM item_instances WHERE id=?", (iid,)).fetchone()[0]
                == 40
            )


def test_missing_authorization_summary_cannot_restore_full_budget(tmp_path, monkeypatch):
    from world_engine import science_budget as budget

    directory = tmp_path / "authorizations"
    monkeypatch.setattr(budget, "AUTHORIZATION_DIRECTORY", directory)
    authorization = {"id": "protected", "max_calls": 60, "token_limit": 600000}
    budget.register_allocation(authorization, tmp_path / "run-one", ())
    (directory / "protected.json").unlink()
    with pytest.raises(ValueError, match="独立分配凭证"):
        budget.budget_lineage((), "world", authorization=authorization)
    with pytest.raises(ValueError, match="独立分配凭证"):
        budget.register_allocation(authorization, tmp_path / "run-two", ())


def test_prepare_and_runner_limit_requests_to_frozen_skilled_participants(
    lab, settings, tmp_path, monkeypatch
):
    from test_science_runner import Backend, runner

    from scripts.demo_science import prepare
    from world_engine import science_budget as budget

    monkeypatch.setattr(budget, "AUTHORIZATION_DIRECTORY", tmp_path / "authorizations")
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        c.execute("UPDATE characters SET skills_json='[]' WHERE id=?", (actors[1],))
    output = tmp_path / "restricted"
    frozen = prepare(
        db.path,
        wid,
        output,
        settings,
        {
            "budget_authorization": {"id": "limited", "max_calls": 60, "token_limit": 600000},
            "participant_policy": "existing_skilled_npcs",
            "initial_ration_quantity": 40,
        },
    )
    assert frozen["verification"]["participants"] == [actors[0]]
    fresh = Database(output / "world.db", legacy_world_defaults=False)
    info = json.loads((output / "run.json").read_text(encoding="utf-8"))
    with FrozenScienceDatabase(fresh.path, session_id=info["session_id"]).write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (info["session_id"],))
        assets = json.loads(c.execute("SELECT assets_json FROM science_workshops").fetchone()[0])
        assert set(assets["actors"]) == {actors[0]}
    backend = Backend()
    r = runner(
        FrozenScienceDatabase(fresh.path, session_id=info["session_id"]),
        backend,
        info["session_id"],
    )
    r.decide(wid, at)
    with fresh.read() as c:
        assert {x[0] for x in c.execute("SELECT actor_id FROM science_model_calls")} == {actors[0]}
