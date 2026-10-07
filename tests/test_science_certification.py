"""哥哥，候选须先登记主张，再用不同实物完成真实对照与复验。"""

import importlib
import importlib.util
import json
from datetime import timedelta

import pytest

pytest_plugins = ["science_fixtures"]


def technologies():
    assert importlib.util.find_spec("world_engine.technologies"), "尚无基于实验证据的科技认证"
    return importlib.import_module("world_engine.technologies").TechnologyService


def declaration(a, *, name="测试内能力", delta=1, temperature=1300):
    return {
        "name": name,
        "problem": "研究一种可重复的承重改进",
        "claim": {
            "metric": "strength",
            "direction": "increase",
            "minimum_delta": delta,
            "comparator_role": "sample",
        },
        "method": {
            "inputs": {
                "sample": "material",
                "gauge": "instrument",
                "kiln": "apparatus",
                "fuel": "energy",
            },
            "steps": [
                {
                    "op": "heat",
                    "input": "sample",
                    "hours": 1,
                    "settings": {"temperature": temperature, "power": 5},
                    "apparatus": "kiln",
                    "energy": "fuel",
                    "result_key": "heated",
                },
                {"op": "assay", "input": "$heated:0", "metric": "strength", "instrument": "gauge"},
            ],
        },
    }


def bindings(a, *, control=False):
    return {
        "sample": a["references"]["clay"] if control else a["materials"]["clay"],
        "gauge": a["instruments"]["strength"],
        "kiln": a["apparatus"],
        "fuel": a["energy"],
    }


def trial(c, sid, actor, cid, a, at, *, revision=1, control=True):
    return technologies().validate(
        c,
        sid,
        actor,
        {
            "op": "validate",
            "candidate_id": cid,
            "revision": revision,
            "bindings": bindings(a),
            "control_bindings": bindings(a, control=control),
        },
        at,
    )


def settle(c, wid, at):
    from test_science_work import finish

    from world_engine.science_programs import ScienceService

    finish(c, wid, at)
    ScienceService.tick(c, wid, at)


def repeat(c, wid, sid, actor, cid, a, at, *, start=0):
    eid = trial(c, sid, actor, cid, a, at + timedelta(hours=start))
    for offset in (0, 1, 1.5, 2):
        moment = at + timedelta(hours=start + offset)
        if offset == 0:
            from world_engine.science_programs import ScienceService

            ScienceService.tick(c, wid, moment)
        else:
            settle(c, wid, moment)
    return eid


def test_candidate_rejects_foreign_missing_and_future_evidence(science_lab):
    service = technologies()
    from world_engine.collective_research import CollectiveResearchService as R

    db, _, sid, actors, assets, at = science_lab
    with db.write() as c:
        foreign = R.record_observation(c, actors[1], at, "别人的私有实测", {"kind": "test"})
        future = R.record_observation(
            c, actors[0], at + timedelta(hours=3), "将来的实测", {"kind": "test"}
        )
        for eid in ("missing", foreign, future):
            with pytest.raises(ValueError, match="证据"):
                service.declare(
                    c,
                    sid,
                    actors[0],
                    declaration(assets["actors"][actors[0]]) | {"evidence_ids": [eid]},
                    at,
                )


def test_words_and_pending_trial_never_certify(science_lab):
    service = technologies()
    db, _, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a), at)
        assert service.certify(c, cid, actors[0], at)["status"] == "needs_evidence"
        trial(c, sid, actors[0], cid, a, at)
        result = service.certify(c, cid, actors[0], at)
        assert result["status"] == "needs_evidence" and result["gaps"]
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0


def test_two_real_samples_and_traditional_control_certify_once(science_lab):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a), at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        assert (
            service.certify(c, cid, actors[0], at + timedelta(hours=2))["status"]
            == "needs_evidence"
        )
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        result = service.certify(c, cid, actors[0], at + timedelta(hours=4))
        assert result["status"] == "verified", result["gaps"]
        assert result["technology_id"]
        assert len(service.list(c, wid)) == 1
        assert (
            service.certify(c, cid, actors[0], at + timedelta(hours=4))["technology_id"]
            == result["technology_id"]
        )
        assert c.execute("SELECT COUNT(*) FROM technology_evidence").fetchone()[0] >= 4


def test_raw_control_cannot_claim_traditional_craft_as_breakthrough(science_lab):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a), at)
        trial(c, sid, actors[0], cid, a, at, control=False)
        for offset in (0, 1, 1.5, 2):
            if offset:
                settle(c, wid, at + timedelta(hours=offset))
            else:
                from world_engine.science_programs import ScienceService

                ScienceService.tick(c, wid, at)
        result = service.certify(c, cid, actors[0], at + timedelta(hours=2))
        assert result["status"] == "needs_evidence"
        assert any("传统" in gap or "能力基线" in gap for gap in result["gaps"])


@pytest.mark.parametrize("delta,precision", [(1000, 0.1), (1, 1000)])
def test_insufficient_or_unresolvable_improvement_is_not_technology(science_lab, delta, precision):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        c.execute(
            "UPDATE collective_instruments SET precision=? WHERE item_id=?",
            (precision, a["instruments"]["strength"]),
        )
        cid = service.declare(c, sid, actors[0], declaration(a, delta=delta), at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        assert (
            service.certify(c, cid, actors[0], at + timedelta(hours=4))["status"]
            == "needs_evidence"
        )


def test_revision_cannot_reuse_old_thresholds_or_edit_old_claim(science_lab):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a, delta=1000), at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        revision = service.revise(
            c, cid, actors[0], declaration(a, delta=1), at + timedelta(hours=4)
        )
        assert revision == 2
        assert (
            service.certify(c, cid, actors[0], at + timedelta(hours=4))["status"]
            == "needs_evidence"
        )
        old = json.loads(
            c.execute(
                (
                    "SELECT spec_json FROM science_candidate_revisions WHERE candidate"
                    "_id=? AND revision=1"
                ),
                (cid,),
            ).fetchone()[0]
        )
        assert old["claim"]["minimum_delta"] == 1000


def test_register_after_real_start_is_rejected(science_lab):
    service = technologies()
    from world_engine.science_programs import ScienceService
    from world_engine.technology_evidence import register_trial

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a), at)
        eid = trial(c, sid, actors[0], cid, a, at)
        ScienceService.tick(c, wid, at)
        control = c.execute(
            "SELECT control_execution_id FROM science_claim_trials WHERE execution_id=?", (eid,)
        ).fetchone()[0]
        with pytest.raises(ValueError, match="登记|开始"):
            register_trial(c, cid, 1, eid, control, at + timedelta(minutes=1))


def test_name_changes_do_not_create_another_technology(science_lab):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a), at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        first = service.certify(c, cid, actors[0], at + timedelta(hours=4))
        assert first["status"] == "verified", first
        other = service.declare(
            c, sid, actors[0], declaration(a, name="近义名称"), at + timedelta(hours=4)
        )
        result = service.certify(c, other, actors[0], at + timedelta(hours=4))
        assert result["status"] == "duplicate" and len(service.list(c, wid)) == 1


def test_claim_conditions_must_be_supported_and_actually_met(science_lab):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        spec = declaration(a)
        spec["claim"]["conditions"] = {"max_temperature": 50}
        cid = service.declare(c, sid, actors[0], spec, at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        result = service.certify(c, cid, actors[0], at + timedelta(hours=4))
        assert result["status"] == "needs_evidence"
        assert any("条件" in g for g in result["gaps"])


def test_real_method_revision_preserves_technology_and_old_version(science_lab):
    service = technologies()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        cid = service.declare(c, sid, actors[0], declaration(a), at)
        repeat(c, wid, sid, actors[0], cid, a, at)
        repeat(c, wid, sid, actors[0], cid, a, at, start=2)
        first = service.certify(c, cid, actors[0], at + timedelta(hours=4))
        assert first["status"] == "verified"
        service.revise(c, cid, actors[0], declaration(a, temperature=1350), at + timedelta(hours=4))
        for start in (4, 6):
            eid = trial(c, sid, actors[0], cid, a, at + timedelta(hours=start), revision=2)
            assert eid
            for offset in (0, 1, 1.5, 2):
                if offset:
                    settle(c, wid, at + timedelta(hours=start + offset))
                else:
                    from world_engine.science_programs import ScienceService

                    ScienceService.tick(c, wid, at + timedelta(hours=start))
        second = service.certify(c, cid, actors[0], at + timedelta(hours=8))
        assert second["status"] == "verified"
        assert second["technology_id"] == first["technology_id"]
        versions = c.execute(
            (
                "SELECT version,method_json FROM technology_versions WHERE technol"
                "ogy_id=? ORDER BY version"
            ),
            (first["technology_id"],),
        ).fetchall()
        assert [r["version"] for r in versions] == [1, 2]
        assert json.loads(versions[0]["method_json"])["steps"][0]["settings"]["temperature"] == 1300
