"""哥哥，生成仅供浏览器检查的隔离库，显示用多父节点不算真实科研成果。"""

import json
from pathlib import Path
from uuid import uuid4

pytest_plugins = ["science_fixtures", "test_science_methods"]


def test_prepare_isolated_browser_fixture(certified):
    from world_engine.repository import WorldRepository
    from world_engine.science_freeze import backup_world, state_digest

    db, wid, _, _, _, _, tid = certified
    target = Path(".superpowers/sdd/2026-10-05-emergent-science")
    copy = target / ("ui-" + uuid4().hex + ".db")
    with db.write() as c:
        empty = WorldRepository().create_world(
            c, name="空科研卷宗", minutes_per_tick=60, seed_demo=False, seed_maps=False
        )
        c.execute(
            "UPDATE technologies SET name=? WHERE id=?",
            ('<img src=x onerror="window.scienceXss=1">测试名称', tid),
        )
    with db.read() as c:
        before = state_digest(c)
    backup_world(db.path, copy)
    with db.read() as c:
        assert state_digest(c) == before
    (target / "ui-fixture.json").write_text(
        json.dumps(
            {
                "database": str(copy.resolve()),
                "world_id": wid,
                "empty_world_id": empty,
                "technology_id": tid,
            }
        ),
        encoding="utf-8",
    )
