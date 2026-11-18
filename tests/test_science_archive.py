"""哥哥，旧源码可以更新，已停止实验的原文和费用仍须完整可核验。"""

import json
from pathlib import Path

import pytest
from test_science_budget_lineage import lineage, prior  # noqa: F401

from world_engine.database import Database
from world_engine.science_freeze import archive_frozen_files, verify_freeze

pytest_plugins = ["science_fixtures"]


def archive(prior):  # noqa: F811
    output, _, sid, _, _ = prior
    db = Database(output / "world.db", legacy_world_defaults=False)
    with db.read() as c:
        archive_frozen_files(c, sid, output)
        frozen = json.loads(c.execute("SELECT freeze_json FROM science_sessions").fetchone()[0])
    code = Path(next(iter(frozen["source_files"])))
    return output, db, sid, code


def test_stopped_archive_keeps_original_cost_after_code_changes(prior):  # noqa: F811
    output, db, sid, code = archive(prior)
    original = (output / "world.db").read_bytes()
    code.write_text("new implementation", encoding="utf-8")
    with db.read() as c:
        assert not verify_freeze(c, sid)
        assert verify_freeze(c, sid, archived_run=output)
    assert lineage((output,), prior[1])["remaining"]["token_limit"] == 178696
    assert (output / "world.db").read_bytes() == original


@pytest.mark.parametrize("change", ["blob", "index", "escape", "state"])
def test_changed_archive_or_accounting_is_rejected(prior, change):  # noqa: F811
    output, db, _, code = archive(prior)
    code.write_text("new implementation", encoding="utf-8")
    index = output / "frozen-files.json"
    mapping = json.loads(index.read_text(encoding="utf-8"))
    if change == "blob":
        (output / "frozen-files" / next(iter(mapping.values()))).write_text("changed")
    elif change == "index":
        index.write_text("{}")
    elif change == "escape":
        mapping[next(iter(mapping))] = "../../prior-code.py"
        index.write_text(json.dumps(mapping))
    else:
        with db.write() as c:
            c.execute("UPDATE science_model_calls SET accounted_tokens=1")
    with pytest.raises(ValueError):
        lineage((output,), prior[1])


def test_active_session_cannot_use_archived_sources(prior):  # noqa: F811
    output, db, sid, code = archive(prior)
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running'")
    code.write_text("new implementation", encoding="utf-8")
    with db.read() as c:
        assert not verify_freeze(c, sid, archived_run=output)
        with pytest.raises(ValueError):
            archive_frozen_files(c, sid, output)
