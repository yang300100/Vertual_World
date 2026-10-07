"""哥哥，用首轮真实失败形态离线核验可用契约与调度。"""

import json

from test_science_programs import craft_program
from test_science_runner import Backend, runner

from world_engine.collective_research import CollectiveResearchService as R
from world_engine.science_programs import ScienceService

pytest_plugins = ["science_fixtures"]


def missing_energy(assets):
    program = craft_program(assets)
    program["commands"][0]["operation"] = "form"
    program["commands"][0]["settings"] = {"pressure": 1, "geometry_ratio": 1}
    program["commands"][0]["energy"] = None
    return program


def test_rejection_identifies_operation_and_missing_energy(science_lab):
    db, _, sid, actors, assets, at = science_lab
    with db.write() as c:
        pid = ScienceService.submit(
            c, sid, actors[0], missing_energy(assets["actors"][actors[0]]), at
        )
        row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        feedback = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE character_id=? "
                "AND kind='science_rejection' ORDER BY rowid DESC LIMIT 1",
                (actors[0],),
            ).fetchone()[0]
        )
        assert row["status"] == "rejected"
        assert "form" in row["error"] and "energy" in row["error"]
        assert feedback["command_index"] == 0
        assert feedback["operation"] == "form"
        assert feedback["submitted_command"]["energy"] is None


def test_rejection_does_not_starve_uncalled_npc(science_lab):
    db, wid, sid, _, assets, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
    b = Backend()
    original = b.complete

    def decide(**kwargs):
        aid = kwargs["user_payload"]["actor_id"]
        b.program = missing_energy(assets["actors"][aid])
        return original(**kwargs)

    b.complete = decide
    r = runner(db, b, sid)
    assert r.decide(wid, at)
    first = b.calls[0]["user_payload"]["actor_id"]
    assert r.decide(wid, at)
    assert b.calls[1]["user_payload"]["actor_id"] != first


def test_real_observation_still_takes_priority_after_rejection(science_lab):
    db, wid, sid, _, assets, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
    b = Backend()
    original = b.complete

    def decide(**kwargs):
        aid = kwargs["user_payload"]["actor_id"]
        b.program = missing_energy(assets["actors"][aid])
        return original(**kwargs)

    b.complete = decide
    r = runner(db, b, sid)
    assert r.decide(wid, at)
    first = b.calls[0]["user_payload"]["actor_id"]
    with db.write() as c:
        R.record_observation(
            c, first, at, "本人确实看到新的读数", {"kind": "observation", "value": 2}
        )
    assert r.decide(wid, at)
    assert b.calls[1]["user_payload"]["actor_id"] == first
