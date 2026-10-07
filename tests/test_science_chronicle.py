"""哥哥，科技与编年必须同事务登记，历史过程按原始记录展开。"""

import importlib
import importlib.util
from datetime import timedelta

import pytest

pytest_plugins = ["science_fixtures", "test_science_methods"]


def chronicle():
    assert importlib.util.find_spec("world_engine.science_chronicle"), "尚无科研过程编年"
    return importlib.import_module("world_engine.science_chronicle")


def test_breakthrough_is_major():
    from world_engine.event_log import importance_for

    assert importance_for("world.technology_breakthrough") == "major"


def test_actual_breakthrough_is_in_chronicle_once_with_evidence_links(certified):
    service = chronicle()
    from world_engine.repository import WorldRepository

    db, wid, _, _, _, at, tid = certified
    with db.write() as c:
        cid = c.execute("SELECT candidate_id FROM technologies WHERE id=?", (tid,)).fetchone()[0]
        event = service.record_breakthrough(c, tid, cid, at)
        assert service.record_breakthrough(c, tid, cid, at) == event
        events = WorldRepository().list_events(c, wid, limit=100, scope="chronicle")
        assert any(e["id"] == event for e in events)
        profile = c.execute("SELECT * FROM event_profiles WHERE event_id=?", (event,)).fetchone()
        assert profile["scope_type"] == "local"
        assert (
            c.execute("SELECT COUNT(*) FROM event_causes WHERE event_id=?", (event,)).fetchone()[0]
            >= 4
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM world_events WHERE event_type='world.technology_breakthrough'"
            ).fetchone()[0]
            == 1
        )


def test_chronicle_failure_rolls_back_technology_registration(science_lab, monkeypatch):
    service = chronicle()
    from test_science_certification import declaration, repeat, technologies

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = technologies().declare(c, sid, actors[0], declaration(a), at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)

        def fail(*args, **kwargs):
            raise ValueError("离线注入编年落库失败")

        monkeypatch.setattr(service, "record_breakthrough", fail)
        with pytest.raises(ValueError, match="落库失败"):
            technologies().certify(c, cid, actors[0], at + timedelta(hours=4))
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM technology_versions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM technology_knowledge").fetchone()[0] == 0


def test_process_has_actual_trials_costs_and_raw_origins(certified):
    service = chronicle()
    db, wid, _, _, _, _, tid = certified
    with db.read() as c:
        process = service.technology_process(c, wid, tid)
    assert process["problem"] and len(process["trials"]) == 2
    assert process["validation"] and process["conditions"]
    assert process["trials"][0]["execution"]["work_ids_json"]
    assert process["trials"][0]["execution"]["ledger_ids_json"]
    assert "prior_knowledge" in process and "hypotheses" in process and "transfers" in process


def test_failed_claim_revision_and_initial_problem_are_kept_and_linked(science_lab):
    import json

    from test_science_certification import declaration, repeat, settle, technologies, trial

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    with db.write() as c:
        initial = declaration(a, temperature=100)
        initial["problem"] = "最初要改进原有承重能力"
        cid = technologies().declare(c, sid, actors[0], initial, at)
        for start in (0, 2):
            repeat(c, wid, sid, actors[0], cid, a, at, start=start)
        assert (
            technologies().certify(c, cid, actors[0], at + timedelta(hours=4))["status"]
            == "needs_evidence"
        )
        revised = declaration(a)
        revised["problem"] = "根据失败记录收窄问题并修订方法"
        revision = technologies().revise(c, cid, actors[0], revised, at + timedelta(hours=4))
        for start in (4, 6):
            moment = at + timedelta(hours=start)
            trial(c, sid, actors[0], cid, a, moment, revision=revision)
            ScienceService.tick(c, wid, moment)
            for offset in (1, 1.5, 2):
                settle(c, wid, moment + timedelta(hours=offset))
        result = technologies().certify(c, cid, actors[0], at + timedelta(hours=8))
        assert result["status"] == "verified", result
        process = chronicle().technology_process(c, wid, result["technology_id"])
        assert process["problem"] == initial["problem"]
        assert len(process["revisions"]) == 2 and len(process["failures"]) >= 2
        assert len(process["trials"]) == 4
        event = c.execute(
            "SELECT event_id FROM science_chronicle_records WHERE technology_id=?",
            (result["technology_id"],),
        ).fetchone()[0]
        causes = {
            r[0]
            for r in c.execute("SELECT cause_event_id FROM event_causes WHERE event_id=?", (event,))
        }
        revision_events = [
            r["id"]
            for r in c.execute(
                "SELECT * FROM world_events WHERE event_type IN "
                "('research.candidate_declared','research.candidate_revised')"
            )
            if json.loads(r["payload_json"])["candidate_id"] == cid
        ]
        assert len(revision_events) == 2 and set(revision_events) <= causes
