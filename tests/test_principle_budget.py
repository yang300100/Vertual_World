"""哥哥，离线费用只验证继承保护，不模拟真实付费科研达成。"""

import json
import sqlite3
from uuid import uuid4

import pytest
from test_principle_research import call, principle_lab  # noqa: F401
from test_principle_runner import Backend

from world_engine.database import Database
from world_engine.principle_budget import lineage_for, register, usage
from world_engine.principle_runner import PrincipleRunner
from world_engine.principle_types import IdeaBatch
from world_engine.repository import to_iso


def setup_lineage(db, wid, sid, actors, at, tmp_path):
    with db.write() as c:
        for _ in range(2):
            call(c, sid, actors[0], "propose", {"reason": "离线计账边界", "ideas": []}, at)
        c.execute("UPDATE principle_sessions SET status='stopped' WHERE id=?", (sid,))
    (db.path.parent / "run.json").write_text(
        json.dumps({"session_id": sid, "database": db.path.name}), encoding="utf-8"
    )
    lineage = lineage_for(db.path.parent, tmp_path / "registry")
    target = tmp_path / "branch.db"
    with sqlite3.connect(db.path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    branch = Database(target, legacy_world_defaults=False)
    child = str(uuid4())
    with branch.write() as c:
        c.execute(
            "INSERT INTO principle_sessions(id,world_id,status,target_count,max_calls,"
            "token_limit,created_at,manifest_json) VALUES (?,?,'running',20,60,600000,?,?)",
            (child, wid, to_iso(at), json.dumps({"budget_lineage": lineage})),
        )
    register(lineage, target, child)
    return lineage, branch, child


def test_new_branch_carries_source_and_current_paid_calls(principle_lab, tmp_path):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    lineage, branch, child = setup_lineage(db, wid, sid, actors, at, tmp_path)
    assert usage(lineage)["calls"] == 2
    with branch.write() as c:
        call(c, child, actors[0], "propose", {"reason": "离线计账边界", "ideas": []}, at)
    assert usage(lineage)["calls"] == 3 and usage(lineage)["accounted_tokens"] == 300


def test_changed_source_receipt_stops_before_backend_request(principle_lab, tmp_path):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    _, branch, child = setup_lineage(db, wid, sid, actors, at, tmp_path)
    with db.write() as c:
        c.execute("UPDATE principle_calls SET accounted_tokens=101 WHERE session_id=?", (sid,))
    backend = Backend()
    with pytest.raises(ValueError, match="来源被改变"):
        PrincipleRunner(branch, backend, child).call(
            "propose", actors[0], {}, IdeaBatch, "离线契约"
        )
    assert backend.calls == []


def test_total_cap_blocks_even_when_local_quota_is_available(principle_lab, tmp_path):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    lineage, branch, child = setup_lineage(db, wid, sid, actors, at, tmp_path)
    lineage["max_calls"] = 2
    with branch.write() as c:
        c.execute(
            "UPDATE principle_sessions SET manifest_json=? WHERE id=?",
            (json.dumps({"budget_lineage": lineage}), child),
        )
    backend = Backend()
    assert PrincipleRunner(branch, backend, child).call(
        "propose", actors[0], {}, IdeaBatch, "离线契约"
    ) == (None, None)
    assert backend.calls == []
    with branch.read() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM principle_calls WHERE session_id=?", (child,)
            ).fetchone()[0]
            == 0
        )


def test_duplicate_billing_branch_is_rejected(principle_lab, tmp_path):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    lineage, branch, child = setup_lineage(db, wid, sid, actors, at, tmp_path)
    with pytest.raises(ValueError, match="重复来源"):
        register(lineage, branch.path, child)


def test_missing_registry_never_falls_back_to_empty_ledger(principle_lab, tmp_path, monkeypatch):  # noqa: F811
    from pathlib import Path

    db, wid, sid, actors, at = principle_lab
    lineage, branch, child = setup_lineage(db, wid, sid, actors, at, tmp_path)
    original = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: False if str(p) == lineage["registry"] else original(p)
    )
    backend = Backend()
    with pytest.raises(ValueError, match="注册表缺失"):
        PrincipleRunner(branch, backend, child).call(
            "propose", actors[0], {}, IdeaBatch, "离线契约"
        )
    assert backend.calls == []


def test_existing_registry_cannot_omit_a_registered_sibling(principle_lab, tmp_path):  # noqa: F811
    from pathlib import Path

    db, wid, sid, actors, at = principle_lab
    lineage, branch, child = setup_lineage(db, wid, sid, actors, at, tmp_path)
    registry = Path(lineage["registry"])
    body = json.loads(registry.read_text(encoding="utf-8"))
    body["runs"] = []
    registry.write_text(json.dumps(body), encoding="utf-8")
    backend = Backend()
    with pytest.raises(ValueError, match="登记凭证不一致"):
        PrincipleRunner(branch, backend, child).call(
            "propose", actors[0], {}, IdeaBatch, "离线契约"
        )
    assert backend.calls == []
