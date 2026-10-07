"""哥哥，观察界面只读，不把作者资料当成科研事实。"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest_plugins = ["science_fixtures", "test_science_methods"]


def client(db):
    from world_engine.api_science import build_science_router

    app = FastAPI()
    app.include_router(build_science_router(db))
    return TestClient(app)


def test_empty_science_view_and_unknown_world_404(science_lab):
    db, wid, _, _, _, _ = science_lab
    api = client(db)
    assert api.get(f"/api/worlds/{wid}/technologies/graph").json() == {"nodes": [], "edges": []}
    assert api.get(f"/api/worlds/{wid}/science").json()["usage"]["calls"] == 0
    assert api.get("/api/worlds/missing/science").status_code == 404
    assert api.get(f"/api/worlds/{wid}/technologies/missing/process").status_code == 404


def test_process_routes_read_only_no_hidden_profile(certified):
    db, wid, _, _, _, _, tid = certified
    api = client(db)
    with db.read() as c:
        before = c.total_changes
        count = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
    response = api.get(f"/api/worlds/{wid}/technologies/{tid}/process")
    assert response.status_code == 200
    assert response.json()["trials"]
    assert "strength_scale" not in response.text and "rules_json" not in response.text
    assert api.get(f"/api/worlds/other/technologies/{tid}/process").status_code == 404
    with db.read() as c:
        assert c.total_changes == before
        assert c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == count


def test_science_summary_omits_freeze_profiles_and_auth(science_lab):
    db, wid, sid, _, _, _ = science_lab
    with db.write() as c:
        c.execute(
            "UPDATE science_sessions SET freeze_json=? WHERE id=?",
            (json.dumps({"rules_json": "author-secret", "api_key": "secret-key"}), sid),
        )
    text = client(db).get(f"/api/worlds/{wid}/science").text
    assert "author-secret" not in text and "secret-key" not in text
